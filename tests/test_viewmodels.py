"""
Unit tests for app/viewmodels.py pure data shaping functions.
"""
from agent.events import RunEvent
from agent.state import (
    Answer,
    AnswerDecision,
    ApprovalItem,
    ApprovalKind,
    Citation,
    PlanStep,
    PostconditionResult,
    PostconditionSpec,
    Question,
    QuestionType,
    RiskLevel,
    RunState,
    RunStatus,
    UnderstoodGoal,
)
from app.viewmodels import (
    build_approval_item_card,
    build_event_timeline,
    build_final_signoff_summary,
    build_goal_card,
    build_plan_checklist,
    build_report_view,
    format_run_status,
)


def test_format_run_status():
    b_comp = format_run_status("completed")
    assert b_comp.label == "Completed"
    assert b_comp.is_terminal is True
    assert b_comp.color == "green"

    b_wait = format_run_status("waiting_approval")
    assert b_wait.label == "Waiting Approval"
    assert b_wait.is_terminal is False

    b_rej = format_run_status("rejected_by_human")
    assert b_rej.is_terminal is True
    assert b_rej.color == "red"

    b_none = format_run_status(None)
    assert b_none.label == "Pending"


def test_build_goal_card():
    assert build_goal_card(None) is None

    goal = UnderstoodGoal(
        customer="Acme Corp",
        questionnaire_path="data/inbox/Acme.xlsx",
        selection_rationale="Newest file mtime",
        deadline="2026-11-01",
        constraints=["No 99.99% uptime"],
        low_confidence_goal=False,
    )
    card = build_goal_card(goal)
    assert card is not None
    assert card.customer == "Acme Corp"
    assert card.chosen_file == "data/inbox/Acme.xlsx"
    assert card.deadline == "2026-11-01"
    assert "No 99.99% uptime" in card.constraints


def test_build_plan_checklist():
    steps = [
        PlanStep(
            id="step-1",
            description="Find inbox files",
            tool_hint="find_inbox_files",
            postcondition=PostconditionSpec(kind="file_exists", params={}),
            status="completed",
        ),
        PlanStep(
            id="step-2",
            description="Verify commitments",
            tool_hint=None,
            postcondition=PostconditionSpec(kind="no_commitment_question_answered", params={}),
            status="in_progress",
        ),
    ]
    vms = build_plan_checklist(steps)
    assert len(vms) == 2
    assert vms[0].is_completed is True
    assert vms[0].is_failed is False
    assert vms[1].is_in_progress is True
    assert vms[1].tool_hint == "Verification Step"


def test_build_event_timeline():
    events = [
        RunEvent(run_id="r1", seq=1, ts="2026-10-13T10:00:00Z", type="run_started", payload={"request_text": "Do Acme"}),
        RunEvent(run_id="r1", seq=2, ts="2026-10-13T10:00:01Z", type="plan_revised", payload={"reason": "Retrying step"}),
        RunEvent(run_id="r1", seq=3, ts="2026-10-13T10:00:02Z", type="run_failed", payload={"failure_reason": "Fatal error"}),
    ]
    rows = build_event_timeline(events)
    assert len(rows) == 3
    assert rows[0].event_type == "run_started"
    assert "Do Acme" in rows[0].summary
    assert rows[1].is_replan is True
    assert rows[2].is_error is True


def test_build_approval_item_card_commitment():
    # Commitment question cannot be approved
    item = ApprovalItem(
        question_id="Q-34",
        question_text="Do you guarantee 99.99% uptime?",
        draft_answer=None,
        evidence=[],
        confidence=0.95,
        flags=["commitment_required: 99.99"],
        reason_for_escalation="Commitment clause requires human sign-off",
    )
    card = build_approval_item_card(item)
    assert card.can_approve is False
    assert "Commitment" in card.cannot_approve_reason
    assert "Commitment" in card.type_badge


def test_build_approval_item_card_no_evidence():
    # Out of scope item with no draft and no citations cannot be approved
    item = ApprovalItem(
        question_id="Q-29",
        question_text="Do you hold FedRAMP High?",
        draft_answer=None,
        evidence=[],
        confidence=0.2,
        flags=[],
        reason_for_escalation="Not in docs",
    )
    card = build_approval_item_card(item)
    assert card.can_approve is False
    assert "No draft answer exists" in card.cannot_approve_reason


def test_build_approval_item_card_valid():
    # Grounded answer can be approved
    item = ApprovalItem(
        question_id="Q-01",
        question_text="Do you require MFA?",
        draft_answer="Yes, MFA is mandatory for all access.",
        evidence=[Citation(doc="Access_Control.md", quote="All users must use MFA")],
        confidence=0.92,
        flags=[],
        reason_for_escalation="Review requested",
    )
    card = build_approval_item_card(item)
    assert card.can_approve is True
    assert card.cannot_approve_reason is None
    assert len(card.citations) == 1


def test_build_final_signoff_summary():
    state = RunState(
        run_id="r-signoff",
        request_text="test",
        questions=[Question(id="Q-01", text="MFA?", raw_row=2)],
        answers=[
            Answer(
                question_id="Q-01",
                text="Yes",
                decision=AnswerDecision.ANSWERED,
                citations=[Citation(doc="Policy.md", quote="Yes")],
                human_override="Approved by CISO",
            )
        ],
    )
    summary = build_final_signoff_summary(state)
    assert summary.total_count == 1
    assert summary.overridden_count == 1
    assert summary.answered_count == 0
    assert len(summary.preview_answers) == 1
    assert summary.preview_answers[0]["Override"] == "Approved by CISO"


def test_build_report_view():
    state = RunState(
        run_id="r-rep",
        request_text="test",
        questions=[Question(id="Q-01", text="MFA?", raw_row=2)],
        answers=[
            Answer(
                question_id="Q-01",
                text="Yes",
                decision=AnswerDecision.ANSWERED,
                citations=[Citation(doc="Policy.md", quote="MFA is enforced")],
            )
        ],
        postcondition_results=[
            PostconditionResult(kind="file_exists", passed=True, evidence="Found file", step_id="step-1")
        ],
    )
    report_dict = {
        "run_id": "r-rep",
        "status": "completed",
        "report_md": "# Report\nSummary",
        "artifacts": {"final_xlsx": "artifacts/r-rep/final.xlsx"},
    }
    rv = build_report_view(report_dict, state)
    assert rv.run_id == "r-rep"
    assert rv.status.label == "Completed"
    assert rv.answered_count == 1
    assert len(rv.postconditions) == 1
    assert len(rv.questions) == 1
    assert rv.questions[0].citations[0].doc == "Policy.md"

