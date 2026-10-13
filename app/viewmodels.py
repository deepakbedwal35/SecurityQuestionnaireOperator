"""
Pure functions transforming service domain models into display-ready structures.
All data shaping is isolated here and 100% unit-testable without Streamlit or SQLite.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime

from agent.events import RunEvent
from agent.state import (
    Answer,
    AnswerDecision,
    ApprovalItem,
    ApprovalKind,
    ApprovalRequest,
    PlanStep,
    PostconditionResult,
    Question,
    QuestionType,
    RiskLevel,
    RunState,
    RunStatus,
    UnderstoodGoal,
)


@dataclass(frozen=True)
class StatusBadgeVM:
    status: str
    label: str
    color: str  # Streamlit-friendly color identifier or css/markdown color
    icon: str
    is_terminal: bool


def format_run_status(status_str: Optional[str]) -> StatusBadgeVM:
    s = (status_str or "").lower()
    if s == RunStatus.COMPLETED.value:
        return StatusBadgeVM(status=s, label="Completed", color="green", icon="✅", is_terminal=True)
    if s == RunStatus.WAITING_APPROVAL.value:
        return StatusBadgeVM(status=s, label="Waiting Approval", color="orange", icon="⏸️", is_terminal=False)
    if s == RunStatus.FAILED.value:
        return StatusBadgeVM(status=s, label="Failed", color="red", icon="❌", is_terminal=True)
    if s == RunStatus.REJECTED_BY_HUMAN.value:
        return StatusBadgeVM(status=s, label="Rejected by Human", color="red", icon="🛑", is_terminal=True)
    if s == RunStatus.RUNNING.value:
        return StatusBadgeVM(status=s, label="Running", color="blue", icon="🔄", is_terminal=False)
    if s == RunStatus.PLANNING.value:
        return StatusBadgeVM(status=s, label="Planning", color="blue", icon="📝", is_terminal=False)
    if s == RunStatus.VERIFYING.value:
        return StatusBadgeVM(status=s, label="Verifying", color="blue", icon="🔍", is_terminal=False)
    if s == RunStatus.ESCALATED.value:
        return StatusBadgeVM(status=s, label="Escalated", color="orange", icon="⚠️", is_terminal=True)
    return StatusBadgeVM(status=s or "unknown", label="Pending", color="gray", icon="⏳", is_terminal=False)


@dataclass(frozen=True)
class GoalCardVM:
    customer: str
    chosen_file: str
    selection_rationale: str
    deadline: str
    constraints: List[str]
    low_confidence: bool
    needs_clarification: bool


def build_goal_card(goal: Optional[UnderstoodGoal]) -> Optional[GoalCardVM]:
    if not goal:
        return None
    return GoalCardVM(
        customer=goal.customer or "Unknown Customer",
        chosen_file=goal.questionnaire_path or "No file chosen",
        selection_rationale=goal.selection_rationale or "Default candidate selected",
        deadline=goal.deadline or "None specified",
        constraints=goal.constraints or [],
        low_confidence=goal.low_confidence_goal,
        needs_clarification=goal.needs_human_clarification,
    )


@dataclass(frozen=True)
class PlanStepVM:
    id: str
    description: str
    tool_hint: str
    postcondition_kind: str
    status: str
    is_completed: bool
    is_failed: bool
    is_in_progress: bool


def build_plan_checklist(plan: List[PlanStep]) -> List[PlanStepVM]:
    vms = []
    for step in plan:
        st = step.status.lower()
        vms.append(PlanStepVM(
            id=step.id,
            description=step.description,
            tool_hint=step.tool_hint or "Verification Step",
            postcondition_kind=step.postcondition.kind,
            status=st,
            is_completed=(st == "completed"),
            is_failed=(st == "failed"),
            is_in_progress=(st == "in_progress"),
        ))
    return vms


@dataclass(frozen=True)
class TimelineRowVM:
    seq: int
    time_str: str
    event_type: str
    tool: Optional[str]
    risk: Optional[str]
    duration_ms: Optional[int]
    summary: str
    is_error: bool
    is_replan: bool
    artifact_path: Optional[str]


def build_event_timeline(events: List[RunEvent]) -> List[TimelineRowVM]:
    rows = []
    for ev in events:
        p = ev.payload or {}
        time_part = ev.ts.split("T")[-1][:8] if "T" in ev.ts else ev.ts

        tool = p.get("tool")
        risk = p.get("risk")
        duration = p.get("duration_ms")
        artifact = p.get("artifact") or p.get("artifact_path") or p.get("output_path")

        # Human-readable summary
        is_error = False
        is_replan = False
        summary = ""

        if ev.type == "run_started":
            summary = f"Run started: {p.get('request_text', '')}"
        elif ev.type == "goal_understood":
            summary = f"Identified customer '{p.get('customer')}' using '{p.get('questionnaire_path')}'"
        elif ev.type == "plan_created":
            steps_cnt = len(p.get("steps", []))
            summary = f"Plan initialized with {steps_cnt} steps"
        elif ev.type == "plan_revised":
            is_replan = True
            summary = f"Replan triggered: {p.get('reason', 'Postcondition retry')}"
        elif ev.type == "tool_called":
            summary = f"Executed {tool} ({risk})"
        elif ev.type == "postcondition_result":
            passed = p.get("passed", False)
            if not passed:
                is_error = True
            summary = f"Postcondition '{p.get('kind')}': {'PASSED' if passed else 'FAILED'} - {p.get('evidence', '')}"
        elif ev.type == "approval_requested":
            summary = f"Approval paused ({p.get('kind')}): {p.get('reason', '')}"
        elif ev.type == "approval_resolved":
            summary = f"Approval resolved by {p.get('decided_by')}: status {p.get('status')}"
        elif ev.type == "injection_flagged":
            is_error = True
            summary = f"Prompt injection neutralized at {p.get('source')}"
        elif ev.type == "run_completed":
            summary = "Run completed successfully. All postconditions verified."
        elif ev.type == "run_failed":
            is_error = True
            summary = f"Run failed: {p.get('failure_reason') or p.get('error')}"
        else:
            summary = str(p)

        rows.append(TimelineRowVM(
            seq=ev.seq,
            time_str=time_part,
            event_type=ev.type,
            tool=tool,
            risk=risk,
            duration_ms=duration,
            summary=summary,
            is_error=is_error,
            is_replan=is_replan,
            artifact_path=artifact,
        ))
    return rows


@dataclass(frozen=True)
class CitationVM:
    doc: str
    quote: str
    chunk_id: Optional[str] = None


@dataclass(frozen=True)
class ApprovalItemCardVM:
    question_id: str
    question_text: str
    draft_answer: Optional[str]
    citations: List[CitationVM]
    confidence: float
    flags: List[str]
    reason_for_escalation: str
    can_approve: bool  # False for commitment questions or items without grounded citations
    cannot_approve_reason: Optional[str]
    type_badge: str


def build_approval_item_card(item: ApprovalItem) -> ApprovalItemCardVM:
    reasons_lower = (item.reason_for_escalation or "").lower()
    is_commitment = "commitment" in reasons_lower or any("commitment" in f.lower() for f in item.flags)
    has_draft = bool(item.draft_answer and item.draft_answer.strip())
    has_citations = len(item.evidence) > 0

    can_approve = True
    cannot_approve_reason = None

    if is_commitment:
        can_approve = False
        cannot_approve_reason = "Commitment/legal questions require explicit human edit or reject per policy."
        type_badge = "Legal/SLA Commitment"
    elif not has_draft:
        can_approve = False
        cannot_approve_reason = "No draft answer exists. Requires human-written answer or reject."
        type_badge = "No Evidence / Out of Scope"
    elif not has_citations:
        can_approve = False
        cannot_approve_reason = "Draft has no grounded citations."
        type_badge = "Ungrounded Draft"
    elif item.confidence < 0.7:
        type_badge = f"Low Confidence ({item.confidence:.2f})"
    elif item.flags:
        type_badge = f"Flagged ({len(item.flags)})"
    else:
        type_badge = "Review"

    citations_vm = [
        CitationVM(doc=c.doc, quote=c.quote, chunk_id=c.chunk_id)
        for c in item.evidence
    ]

    return ApprovalItemCardVM(
        question_id=item.question_id,
        question_text=item.question_text,
        draft_answer=item.draft_answer,
        citations=citations_vm,
        confidence=item.confidence,
        flags=item.flags,
        reason_for_escalation=item.reason_for_escalation or "Human review required",
        can_approve=can_approve,
        cannot_approve_reason=cannot_approve_reason,
        type_badge=type_badge,
    )


@dataclass(frozen=True)
class FinalSignoffSummaryVM:
    answered_count: int
    overridden_count: int
    unresolved_count: int
    total_count: int
    review_xlsx_path: Optional[str]
    preview_answers: List[Dict[str, Any]]


def build_final_signoff_summary(state: Optional[RunState]) -> FinalSignoffSummaryVM:
    if not state:
        return FinalSignoffSummaryVM(0, 0, 0, 0, None, [])

    ans_list = state.answers
    total = len(ans_list)
    answered = sum(1 for a in ans_list if a.decision == AnswerDecision.ANSWERED and not a.human_override)
    overridden = sum(1 for a in ans_list if a.human_override)
    unresolved = sum(1 for a in ans_list if a.decision in (AnswerDecision.ESCALATED, AnswerDecision.FLAGGED))

    preview = []
    q_map = {q.id: q for q in state.questions}
    for a in ans_list:
        q_obj = q_map.get(a.question_id)
        preview.append({
            "ID": a.question_id,
            "Question": q_obj.text if q_obj else a.question_id,
            "Decision": a.decision.value,
            "Answer": a.text[:100] + ("..." if len(a.text) > 100 else ""),
            "Override": a.human_override or "No",
            "Citations": ", ".join(c.doc for c in a.citations) if a.citations else "None",
        })

    review_path = state.artifacts.get("filled_xlsx") or "data/output/Acme_Security_Questionnaire_Filled.xlsx"

    return FinalSignoffSummaryVM(
        answered_count=answered,
        overridden_count=overridden,
        unresolved_count=unresolved,
        total_count=total,
        review_xlsx_path=review_path,
        preview_answers=preview,
    )


@dataclass(frozen=True)
class QuestionDetailVM:
    id: str
    text: str
    decision: str
    answer_text: str
    confidence: float
    citations: List[CitationVM]
    human_override: Optional[str]
    conflict_note: Optional[str]
    flags: List[str]


@dataclass(frozen=True)
class ReportViewVM:
    run_id: str
    status: StatusBadgeVM
    failure_reason: Optional[str]
    parsed_count: int
    processed_count: int
    answered_count: int
    escalated_count: int
    flagged_count: int
    unresolved_count: int
    postconditions: List[Dict[str, Any]]
    questions: List[QuestionDetailVM]
    artifacts: Dict[str, str]
    raw_markdown: str


def build_report_view(report_data: Dict[str, Any], state: Optional[RunState]) -> ReportViewVM:
    rid = report_data.get("run_id", "unknown")
    st_val = report_data.get("status", "unknown")
    st_badge = format_run_status(st_val)
    failure = state.failure_reason if state else None
    artifacts = report_data.get("artifacts", {})
    raw_md = report_data.get("report_md", "")

    if state:
        q_map = {q.id: q for q in state.questions}
        parsed = len(state.questions)
        processed = len(state.answers)
        answered = sum(1 for a in state.answers if a.decision == AnswerDecision.ANSWERED)
        escalated = sum(1 for a in state.answers if a.decision == AnswerDecision.ESCALATED)
        flagged = sum(1 for a in state.answers if a.decision == AnswerDecision.FLAGGED)
        unresolved = sum(1 for a in state.answers if a.decision in (AnswerDecision.ESCALATED, AnswerDecision.FLAGGED))

        q_vms = []
        for a in state.answers:
            q_obj = q_map.get(a.question_id)
            cits = [CitationVM(doc=c.doc, quote=c.quote, chunk_id=c.chunk_id) for c in a.citations]
            q_vms.append(QuestionDetailVM(
                id=a.question_id,
                text=q_obj.text if q_obj else a.question_id,
                decision=a.decision.value,
                answer_text=a.text,
                confidence=a.confidence,
                citations=cits,
                human_override=a.human_override,
                conflict_note=a.conflict_note,
                flags=q_obj.flags if q_obj else [],
            ))

        post_rows = [
            {
                "Step": r.step_id or "Verification",
                "Kind": r.kind,
                "Passed": "PASS" if r.passed else "FAIL",
                "Evidence": r.evidence,
                "Detail": r.detail or "",
            }
            for r in state.postcondition_results
        ]
    else:
        parsed = processed = answered = escalated = flagged = unresolved = 0
        q_vms = []
        post_rows = []

    return ReportViewVM(
        run_id=rid,
        status=st_badge,
        failure_reason=failure,
        parsed_count=parsed,
        processed_count=processed,
        answered_count=answered,
        escalated_count=escalated,
        flagged_count=flagged,
        unresolved_count=unresolved,
        postconditions=post_rows,
        questions=q_vms,
        artifacts=artifacts,
        raw_markdown=raw_md,
    )

