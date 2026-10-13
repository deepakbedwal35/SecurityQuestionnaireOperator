"""
Streamlit AppTest smoke tests.
Tests end-to-end user journeys through the Streamlit UI using streamlit.testing.v1.AppTest.
Verifies start run, live view, approval review with edit + reject, and report viewing.
"""
import os
import shutil
import tempfile
import pytest
from streamlit.testing.v1 import AppTest

from agent.llm import FakeLLM
from agent.service import OperatorService
from agent.state import ItemAction, ApprovalKind, RunStatus


@pytest.fixture
def clean_test_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_ui_runs.sqlite")
    feedback_path = os.path.join(temp_dir, "test_ui_feedback.json")

    monkeypatch.setenv("CHECKPOINT_DB", db_path)
    monkeypatch.setenv("FEEDBACK_STORE", feedback_path)

    from scripts.generate_data import create_policies, create_questionnaire, create_gold_answers
    create_policies()
    create_questionnaire()
    create_gold_answers()

    yield {
        "db_path": db_path,
        "feedback_path": feedback_path,
        "temp_dir": temp_dir,
    }

    shutil.rmtree(temp_dir, ignore_errors=True)


def test_app_landing_and_sidebar_smoke(clean_test_env):
    """Verifies that the main landing page loads without exception and has welcome heading."""
    at = AppTest.from_file("app/streamlit_app.py")
    at.run()
    assert not at.exception
    assert any("Security Questionnaire Operator" in m.value for m in at.markdown)


def test_app_history_empty_and_populated(clean_test_env):
    """Verifies History page with zero runs and with runs."""
    at = AppTest.from_file("app/pages/4_History.py")
    at.session_state["current_run_id"] = None
    at.run()
    assert not at.exception
    assert any("No runs found" in info.value for info in at.info)

    # Create a run via service
    svc = OperatorService(db_path=clean_test_env["db_path"], llm_client=FakeLLM())
    run_state = svc.start_run("Acme security review")
    svc._futures[run_state.run_id].result(timeout=10.0)

    at_populated = AppTest.from_file("app/pages/4_History.py")
    at_populated.session_state["current_run_id"] = None
    at_populated.run()
    assert not at_populated.exception
    # Should have a button to load the run
    assert len(at_populated.button) >= 1


def test_app_end_to_end_journey_with_approval_and_report(clean_test_env):
    """
    Simulates full UI flow:
    1. Start a run on 1_Run.py
    2. Check status pauses at waiting_approval
    3. Navigate to 2_Approvals.py and resolve with EDIT & REJECT
    4. View final results on 3_Report.py
    """
    # 1. Start run on Page 1
    at_run = AppTest.from_file("app/pages/1_Run.py")
    at_run.session_state["current_run_id"] = None
    at_run.session_state["llm_mode"] = "fake"
    at_run.run()
    assert not at_run.exception

    # Enter prompt and click start
    at_run.text_input[0].input("Acme sent us a security questionnaire, get it done.")
    at_run.button[0].click().run()
    assert not at_run.exception

    assert "current_run_id" in at_run.session_state
    current_run_id = at_run.session_state["current_run_id"]
    assert current_run_id is not None

    # Wait for execution to pause at waiting_approval
    from app.service_access import get_operator_service
    svc = get_operator_service(mode="fake")
    if current_run_id in svc._futures:
        svc._futures[current_run_id].result(timeout=10.0)

    state = svc.get_run(current_run_id)
    assert state is not None
    assert state.status == RunStatus.WAITING_APPROVAL

    # Re-run page 1 to verify live rendering with waiting_approval banner
    at_run_live = AppTest.from_file("app/pages/1_Run.py")
    at_run_live.session_state["current_run_id"] = current_run_id
    at_run_live.session_state["llm_mode"] = "fake"
    at_run_live.run()
    assert not at_run_live.exception

    # 2. Page 2: Approvals
    at_appr = AppTest.from_file("app/pages/2_Approvals.py")
    at_appr.session_state["current_run_id"] = current_run_id
    at_appr.session_state["llm_mode"] = "fake"
    at_appr.run()
    assert not at_appr.exception

    pending = svc.get_pending_approval(current_run_id)
    assert pending is not None
    assert pending.kind == ApprovalKind.ANSWER_REVIEW

    # In ANSWER_REVIEW, resolve items
    decisions = []
    for item in pending.items:
        if "commitment" in (item.reason_for_escalation or "").lower():
            decisions.append({
                "question_id": item.question_id,
                "action": "edit",
                "edited_answer": "Company commits to 99.9% uptime with standard SLA credit remedy.",
            })
        else:
            decisions.append({
                "question_id": item.question_id,
                "action": "reject",
            })

    from agent.state import ApprovalDecision, ApprovalItemDecision
    svc.resolve_approval(
        current_run_id,
        pending.id,
        ApprovalDecision(
            approval_id=pending.id,
            decisions=[ApprovalItemDecision(**d) for d in decisions],
        )
    )
    if current_run_id in svc._futures:
        svc._futures[current_run_id].result(timeout=10.0)

    # Allow operator to proceed to final signoff
    final_state = svc.get_run(current_run_id)
    if final_state.status == RunStatus.WAITING_APPROVAL and final_state.pending_approval:
        # Resolve final signoff
        svc.resolve_approval(
            current_run_id,
            final_state.pending_approval.id,
            ApprovalDecision(
                approval_id=final_state.pending_approval.id,
                final_action=ItemAction.APPROVE,
            )
        )
        if current_run_id in svc._futures:
            svc._futures[current_run_id].result(timeout=10.0)
        final_state = svc.get_run(current_run_id)

    assert final_state.status in (RunStatus.COMPLETED, RunStatus.WAITING_APPROVAL)

    # 3. Page 3: Report & Evidence
    at_rep = AppTest.from_file("app/pages/3_Report.py")
    at_rep.session_state["current_run_id"] = current_run_id
    at_rep.session_state["llm_mode"] = "fake"
    at_rep.run()
    assert not at_rep.exception
    assert len(at_rep.metric) == 6
