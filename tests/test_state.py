import pytest
from datetime import datetime
from agent.state import (
    RunState,
    RunStatus,
    Question,
    QuestionType,
    Answer,
    AnswerDecision,
    Citation,
    PlanStep,
    ApprovalRequest,
    RiskLevel,
)


def test_run_state_initialization():
    state = RunState(
        run_id="run-001",
        request_text="Acme questionnaire",
    )
    assert state.run_id == "run-001"
    assert state.status == RunStatus.PENDING
    assert len(state.questions) == 0
    assert len(state.answers) == 0
    assert state.counters.step_count == 0


def test_question_and_answer_models():
    q = Question(
        id="Q-01",
        text="Do you enforce MFA?",
        type=QuestionType.FACTUAL,
        raw_row=2,
    )
    assert q.id == "Q-01"
    assert q.type == QuestionType.FACTUAL

    ans = Answer(
        question_id="Q-01",
        text="Yes, MFA is enforced via FIDO2 WebAuthn.",
        citations=[Citation(doc="Information_Security_Policy.md", quote="All production access requires MFA")],
        confidence=0.98,
        decision=AnswerDecision.ANSWERED,
    )
    assert ans.decision == AnswerDecision.ANSWERED
    assert len(ans.citations) == 1
    assert ans.citations[0].doc == "Information_Security_Policy.md"


def test_approval_request_model():
    req = ApprovalRequest(
        id="APP-101",
        reason="Submission on customer portal is WRITE_HIGH",
        risk=RiskLevel.WRITE_HIGH,
        approved=False,
    )
    assert req.approved is False
    assert req.risk == RiskLevel.WRITE_HIGH

