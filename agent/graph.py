"""
LangGraph definition: understand -> plan -> execute (questions) -> answer_review (interrupt if escalations)
-> execute (write_filled) -> final_signoff (interrupt) -> execute (finalize_output) -> verify_final -> report.
Using StateGraph with Pydantic RunState and SQLite checkpointer.
"""
import os
import uuid
from typing import Any, Dict, List, Literal, Optional
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt

from agent.events import EventSink
from agent.executor import Executor
from agent.llm import LLMClient
from agent.memory import FeedbackRule, FeedbackStore
from agent.planner import Planner
from agent.policy import PolicyEngine
from agent.report import generate_reports
from agent.state import (
    Answer,
    AnswerDecision,
    ApprovalDecision,
    ApprovalItem,
    ApprovalItemDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    Citation,
    ItemAction,
    QuestionType,
    RiskLevel,
    RunState,
    RunStatus,
)
from agent.tools.registry import ToolRegistry
from agent.tools.retrieve import Retriever
from agent.understand import UnderstandNode
from agent.verifier import Verifier


def _has_items_needing_review(state: RunState) -> bool:
    """
    Returns True if any processed question is escalated, flagged, commitment-type, or low confidence (<0.7).
    """
    q_map = {q.id: q for q in state.questions}
    for ans in state.answers:
        q = q_map.get(ans.question_id)
        if ans.decision in (AnswerDecision.ESCALATED, AnswerDecision.FLAGGED):
            return True
        if q and q.type == QuestionType.COMMITMENT:
            return True
        if ans.confidence < 0.7:
            return True
        if q and q.flags:
            return True
    return False


def create_agent_graph(
    llm_client: LLMClient,
    registry: ToolRegistry,
    retriever: Retriever,
    feedback_store: Optional[FeedbackStore] = None,
    event_sink: Optional[EventSink] = None,
    checkpointer: Any = None,
    enable_interrupts: Optional[bool] = None,
    mailbox: Any = None,
) -> Any:
    # If enable_interrupts not explicitly specified, enable only when checkpointer is provided
    if enable_interrupts is None:
        enable_interrupts = checkpointer is not None
    fb_store = feedback_store or FeedbackStore()
    understand_node = UnderstandNode(llm_client, registry)
    planner_node = Planner(llm_client, registry, retriever)
    executor_node = Executor(registry, llm_client, planner_node)

    from agent.intake import IntakeNode
    intake_node = IntakeNode(llm_client, registry, mailbox=mailbox)

    def node_understand(state: RunState) -> RunState:
        if event_sink:
            event_sink.emit(state.run_id, "run_started", {"request_text": state.request_text})

        # Preflight validation: check configured models before planning
        try:
            llm_client.validate_configuration()
        except Exception as e:
            state.status = RunStatus.FAILED
            state.failure_reason = f"configuration error: {e}"
            if event_sink:
                event_sink.emit(state.run_id, "run_failed", {"failure_reason": state.failure_reason})
            return state

        # Use unified intake node to handle both email and uploaded questionnaire sources
        intake_node.run(state)
        if event_sink and state.understood_goal:
            event_sink.emit(state.run_id, "goal_understood", state.understood_goal.model_dump())
        return state

    def node_plan(state: RunState) -> RunState:
        planner_node.plan(state)
        if event_sink:
            event_sink.emit(state.run_id, "plan_created", {"steps": [s.model_dump() for s in state.plan]})
        return state

    def node_execute(state: RunState) -> RunState:
        executor_node.run_loop(state)
        return state

    def node_answer_review(state: RunState) -> RunState:
        """
        Batched human-in-the-loop review for escalated, flagged, commitment, or low-confidence questions.
        """
        q_map = {q.id: q for q in state.questions}
        review_items: List[ApprovalItem] = []

        for ans in state.answers:
            q = q_map.get(ans.question_id)
            is_commit = q and q.type == QuestionType.COMMITMENT
            needs_review = (
                ans.decision in (AnswerDecision.ESCALATED, AnswerDecision.FLAGGED)
                or is_commit
                or ans.confidence < 0.7
                or (q and len(q.flags) > 0)
            )
            if needs_review:
                reasons = []
                if is_commit:
                    reasons.append("Commitment/legal clause requires explicit review")
                if ans.decision == AnswerDecision.ESCALATED:
                    reasons.append("Answer was escalated")
                if ans.decision == AnswerDecision.FLAGGED:
                    reasons.append("Answer or question was flagged")
                if ans.confidence < 0.7:
                    reasons.append(f"Low confidence ({ans.confidence})")
                if q and q.flags:
                    reasons.append(f"Flags: {', '.join(q.flags)}")

                review_items.append(
                    ApprovalItem(
                        question_id=ans.question_id,
                        question_text=q.text if q else ans.question_id,
                        draft_answer=ans.text if ans.decision == AnswerDecision.ANSWERED else None,
                        evidence=ans.citations,
                        confidence=ans.confidence,
                        flags=q.flags if q else [],
                        reason_for_escalation="; ".join(reasons),
                    )
                )

        if not review_items:
            return state

        # Determine approval request ID (reuse if already requested in event_sink)
        existing_ev = event_sink.get_latest_event(state.run_id, "approval_requested") if event_sink else None
        existing_appr_id = existing_ev.payload.get("id") if (existing_ev and existing_ev.payload.get("kind") == ApprovalKind.ANSWER_REVIEW.value) else None
        appr_id = existing_appr_id or f"appr-ans-{uuid.uuid4().hex[:8]}"

        approval_req = ApprovalRequest(
            id=appr_id,
            run_id=state.run_id,
            kind=ApprovalKind.ANSWER_REVIEW,
            reason=f"Batched review for {len(review_items)} questions requiring human review/sign-off",
            items=review_items,
            risk=RiskLevel.WRITE_HIGH,
            status=ApprovalStatus.PENDING,
        )
        state.pending_approval = approval_req
        state.status = RunStatus.WAITING_APPROVAL
        if event_sink and not event_sink.has_event(state.run_id, "approval_requested", approval_req.id):
            event_sink.emit(state.run_id, "approval_requested", approval_req.model_dump(mode="json"))

        # Pause execution using LangGraph interrupt
        raw_decision = interrupt({
            "approval_id": approval_req.id,
            "kind": approval_req.kind.value,
            "reason": approval_req.reason,
            "items": [it.model_dump() for it in review_items],
        })

        # Process human decision upon resume
        decision_obj: ApprovalDecision
        if isinstance(raw_decision, ApprovalDecision):
            decision_obj = raw_decision
        elif isinstance(raw_decision, dict):
            decision_obj = ApprovalDecision(**raw_decision)
        else:
            raise ValueError(f"Unexpected decision payload format: {type(raw_decision)}")

        item_decisions = decision_obj.decisions or []
        item_decision_map = {d.question_id: d for d in item_decisions}

        all_approved = True
        for it in review_items:
            d = item_decision_map.get(it.question_id)
            if not d:
                continue

            target_ans = next((a for a in state.answers if a.question_id == it.question_id), None)
            if not target_ans:
                continue

            if d.action == ItemAction.APPROVE:
                # Approve draft answer
                target_ans.decision = AnswerDecision.ANSWERED
                target_ans.human_override = f"Approved draft by {decision_obj.decided_by}"
            elif d.action == ItemAction.EDIT:
                edited_text = d.edited_answer or ""
                target_ans.text = edited_text
                target_ans.decision = AnswerDecision.ANSWERED
                target_ans.confidence = 1.0
                target_ans.human_override = f"Human override by {decision_obj.decided_by}"
                target_ans.citations = [
                    Citation(doc="feedback_rules.json", quote=edited_text[:140])
                ]
                # Store in FeedbackStore for future runs
                fb_rule = FeedbackRule(
                    id=f"rule-{uuid.uuid4().hex[:8]}",
                    question=it.question_text,
                    answer=edited_text,
                    approver=decision_obj.decided_by,
                    notes=d.note,
                )
                fb_store.save_rule(fb_rule)
            elif d.action == ItemAction.REJECT:
                target_ans.decision = AnswerDecision.ESCALATED
                target_ans.human_override = f"Rejected by {decision_obj.decided_by}: left unresolved"
                all_approved = False

        approval_req.status = ApprovalStatus.APPROVED if all_approved else ApprovalStatus.EDITED
        approval_req.resolved_by = decision_obj.decided_by
        approval_req.resolved_at = PolicyEngine.utcnow() if hasattr(PolicyEngine, "utcnow") else None
        approval_req.resolution_note = f"Resolved with {len(item_decisions)} item decisions"
        state.pending_approval = approval_req
        state.status = RunStatus.RUNNING

        if event_sink:
            event_sink.emit(state.run_id, "approval_resolved", {
                "approval_id": approval_req.id,
                "status": approval_req.status.value,
                "decided_by": decision_obj.decided_by,
            })

        return state

    def node_final_signoff(state: RunState) -> RunState:
        """
        Mandatory approval before executing WRITE_HIGH finalize_output.
        """
        # Determine approval request ID (reuse if already requested in event_sink)
        existing_ev = event_sink.get_latest_event(state.run_id, "approval_requested") if event_sink else None
        existing_appr_id = existing_ev.payload.get("id") if (existing_ev and existing_ev.payload.get("kind") == ApprovalKind.FINAL_SIGNOFF.value) else None
        appr_id = existing_appr_id or f"appr-final-{uuid.uuid4().hex[:8]}"

        # If delivery is email_reply, draft reply email before interrupt if not already drafted
        if state.understood_goal and state.understood_goal.delivery == "email_reply":
            filled_source = state.artifacts.get("filled_xlsx") or (f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx")
            if "draft_reply" in registry._tools and state.understood_goal.source_message_id and not state.artifacts.get("email_draft_hash"):
                try:
                    registry.execute(
                        "draft_reply",
                        state,
                        message_id=state.understood_goal.source_message_id,
                        attachment_path=filled_source,
                    )
                except Exception:
                    pass

        import hashlib
        import json
        current_answers_repr = json.dumps([{"q": a.question_id, "t": a.text} for a in state.answers], sort_keys=True)
        answers_hash = state.artifacts.get("email_draft_hash") or hashlib.sha256(current_answers_repr.encode("utf-8")).hexdigest()

        approval_req = ApprovalRequest(
            id=appr_id,
            run_id=state.run_id,
            kind=ApprovalKind.FINAL_SIGNOFF,
            reason="Mandatory final sign-off before publishing final artifacts",
            items=[],
            risk=RiskLevel.WRITE_HIGH,
            status=ApprovalStatus.PENDING,
            approved_answers_hash=answers_hash,
        )
        state.pending_approval = approval_req
        state.status = RunStatus.WAITING_APPROVAL
        if event_sink and not event_sink.has_event(state.run_id, "approval_requested", approval_req.id):
            event_sink.emit(state.run_id, "approval_requested", approval_req.model_dump(mode="json"))

        # Pause execution using LangGraph interrupt
        raw_decision = interrupt({
            "approval_id": approval_req.id,
            "kind": approval_req.kind.value,
            "reason": approval_req.reason,
            "email_draft": {
                "to": state.artifacts.get("email_draft_to"),
                "subject": state.artifacts.get("email_draft_subject"),
                "body": state.artifacts.get("email_draft_body"),
                "attachment": state.artifacts.get("email_draft_attachment"),
                "draft_hash": state.artifacts.get("email_draft_hash"),
            } if state.artifacts.get("email_draft_to") else None,
        })

        decision_obj: ApprovalDecision
        if isinstance(raw_decision, ApprovalDecision):
            decision_obj = raw_decision
        elif isinstance(raw_decision, dict):
            decision_obj = ApprovalDecision(**raw_decision)
        else:
            raise ValueError(f"Unexpected decision payload format: {type(raw_decision)}")

        final_action = decision_obj.final_action or ItemAction.APPROVE
        if final_action == ItemAction.REJECT:
            approval_req.status = ApprovalStatus.REJECTED
            approval_req.resolved_by = decision_obj.decided_by
            state.pending_approval = approval_req
            state.status = RunStatus.REJECTED_BY_HUMAN
            state.failure_reason = f"Final sign-off rejected by {decision_obj.decided_by}"
            if event_sink:
                event_sink.emit(state.run_id, "approval_resolved", {
                    "approval_id": approval_req.id,
                    "status": "rejected",
                    "decided_by": decision_obj.decided_by,
                })
            return state

        # Approved
        approval_req.status = ApprovalStatus.APPROVED
        approval_req.resolved_by = decision_obj.decided_by
        approval_req.resolution_note = "Final sign-off granted"
        state.pending_approval = approval_req
        state.status = RunStatus.RUNNING

        if event_sink:
            event_sink.emit(state.run_id, "approval_resolved", {
                "approval_id": approval_req.id,
                "status": "approved",
                "decided_by": decision_obj.decided_by,
            })

        # Execute WRITE_HIGH finalize_output tool
        try:
            from agent.config import get_artifacts_dir
            filled_source = state.artifacts.get("filled_xlsx") or (f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx")
            final_xlsx_dest = os.path.join(get_artifacts_dir(), state.run_id, "final.xlsx")
            registry.execute(
                "finalize_output",
                state,
                source_file=filled_source,
                destination_file=final_xlsx_dest,
            )
            if event_sink:
                event_sink.emit(state.run_id, "tool_called", {
                    "tool": "finalize_output",
                    "artifact": final_xlsx_dest,
                })

            # If email delivery, execute send_reply tool
            if state.understood_goal and state.understood_goal.delivery == "email_reply" and "send_reply" in registry._tools:
                registry.execute(
                    "send_reply",
                    state,
                    message_id=state.understood_goal.source_message_id,
                    attachment_path=final_xlsx_dest,
                )
                if event_sink:
                    event_sink.emit(state.run_id, "tool_called", {
                        "tool": "send_reply",
                        "sent_path": state.artifacts.get("sent_email_path"),
                    })
        except Exception as e:
            state.status = RunStatus.FAILED
            state.failure_reason = f"finalize_output or send_reply failed: {e}"

        return state

    def node_fill_portal(state: RunState) -> RunState:
        """
        Executes observe-act portal loop to fill approved answers and submit to mock customer portal.
        """
        portal_target = os.environ.get("PORTAL_URL")
        # Only execute portal loop if PORTAL_URL is explicitly set and reachable
        if not portal_target:
            return state

        # Check reachability before attempting browser automation
        import urllib.request
        base_portal = "/".join(portal_target.split("/")[:3]) if "://" in portal_target else portal_target
        try:
            with urllib.request.urlopen(f"{base_portal}/login", timeout=1.0):
                pass
        except Exception:
            # Portal server is not running; skip portal submission node
            return state

        # Ensure form URL is targeted if only base URL provided
        target_form_url = portal_target
        if "/questionnaires/" not in target_form_url:
            target_form_url = f"{base_portal}/questionnaires/acme-sec-2024"

        from agent.portal_loop import PortalOperatorLoop
        loop = PortalOperatorLoop(llm=llm_client, portal_url=target_form_url)
        result = loop.run(state)

        if result.status == "completed":
            state.portal_state.submission_id = result.submission_id
        elif result.status == "already_submitted":
            state.portal_state.submission_id = result.submission_id
            state.portal_state.already_submitted = True
        elif result.status == "escalated":
            state.status = RunStatus.ESCALATED
            state.failure_reason = result.failure_reason
        elif result.status == "failed":
            state.status = RunStatus.FAILED
            state.failure_reason = result.failure_reason

        return state

    def node_verify_final(state: RunState) -> RunState:
        # Final sanity verification of completed status
        if state.status not in (RunStatus.FAILED, RunStatus.ESCALATED, RunStatus.REJECTED_BY_HUMAN):
            from agent.config import get_artifacts_dir
            from agent.state import PostconditionSpec
            # If final.xlsx was produced (e.g. via finalize_output), verify it; otherwise verify last step postcondition
            final_xlsx_path = os.path.join(get_artifacts_dir(), state.run_id, "final.xlsx")
            if os.path.exists(final_xlsx_path):
                res = Verifier.verify(PostconditionSpec(kind="file_exists", params={"path": final_xlsx_path}), state)
            else:
                res = Verifier.verify(state.plan[-1].postcondition, state)

            if not res.passed:
                state.status = RunStatus.FAILED
                state.failure_reason = f"Final verification failed: {res.evidence}"
                return state

            # If email delivery, verify reply in thread and attachment matches
            if state.understood_goal and state.understood_goal.delivery == "email_reply":
                res_thread = Verifier.verify(PostconditionSpec(kind="reply_in_correct_thread", params={}), state)
                res_att = Verifier.verify(PostconditionSpec(kind="reply_attachment_matches_final", params={"final_xlsx": final_xlsx_path}), state)
                res_notes = Verifier.verify(PostconditionSpec(kind="no_internal_notes_in_email", params={}), state)
                if not (res_thread.passed and res_att.passed and res_notes.passed):
                    state.status = RunStatus.FAILED
                    state.failure_reason = f"Email delivery verification failed: {res_thread.evidence}; {res_att.evidence}; {res_notes.evidence}"
                    return state

            state.status = RunStatus.COMPLETED
        return state

    def node_report(state: RunState) -> RunState:
        generate_reports(state)
        # Clean up browser context on completion
        try:
            from agent.tools.portal import PortalBrowserManager
            PortalBrowserManager.get_instance().close_run(state.run_id, state)
        except Exception:
            pass
        if event_sink:
            if state.status == RunStatus.COMPLETED:
                event_sink.emit(state.run_id, "run_completed", {"artifacts": state.artifacts})
            else:
                event_sink.emit(state.run_id, "run_failed", {
                    "status": state.status.value,
                    "failure_reason": state.failure_reason,
                })
        return state

    def should_continue_after_understand(state: RunState) -> Literal["plan", "report"]:
        if state.status in (RunStatus.ESCALATED, RunStatus.FAILED):
            return "report"
        return "plan"

    def should_continue_after_execute(state: RunState) -> Literal["answer_review", "final_signoff", "verify_final", "report"]:
        if state.status == RunStatus.FAILED:
            return "report"
        if not enable_interrupts:
            return "verify_final"
        if _has_items_needing_review(state):
            return "answer_review"
        return "final_signoff"

    def should_continue_after_final_signoff(state: RunState) -> Literal["fill_portal", "report"]:
        if state.status in (RunStatus.FAILED, RunStatus.REJECTED_BY_HUMAN):
            return "report"
        return "fill_portal"

    def should_continue_after_fill_portal(state: RunState) -> Literal["verify_final", "report"]:
        if state.status in (RunStatus.FAILED, RunStatus.ESCALATED):
            return "report"
        return "verify_final"

    workflow = StateGraph(RunState)

    workflow.add_node("understand", node_understand)
    workflow.add_node("plan", node_plan)
    workflow.add_node("execute", node_execute)
    workflow.add_node("answer_review", node_answer_review)
    workflow.add_node("final_signoff", node_final_signoff)
    workflow.add_node("fill_portal", node_fill_portal)
    workflow.add_node("verify_final", node_verify_final)
    workflow.add_node("report", node_report)

    workflow.set_entry_point("understand")

    workflow.add_conditional_edges(
        "understand",
        should_continue_after_understand,
        {"plan": "plan", "report": "report"},
    )
    workflow.add_edge("plan", "execute")
    workflow.add_conditional_edges(
        "execute",
        should_continue_after_execute,
        {
            "answer_review": "answer_review",
            "final_signoff": "final_signoff",
            "verify_final": "verify_final",
            "report": "report",
        },
    )
    workflow.add_edge("answer_review", "final_signoff")
    workflow.add_conditional_edges(
        "final_signoff",
        should_continue_after_final_signoff,
        {"fill_portal": "fill_portal", "report": "report"},
    )
    workflow.add_conditional_edges(
        "fill_portal",
        should_continue_after_fill_portal,
        {"verify_final": "verify_final", "report": "report"},
    )
    workflow.add_edge("verify_final", "report")
    workflow.add_edge("report", END)

    return workflow.compile(checkpointer=checkpointer)
