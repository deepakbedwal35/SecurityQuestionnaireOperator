"""
Step 3 Test Suite: Checkpointing, approval interrupt, and API.
Tests:
1. pause_at_interrupt: run stops at ANSWER_REVIEW with status waiting_approval.
2. resume_with_approval: resume proceeds to next node.
3. persistence_across_restart: DB closed, new graph instantiated from scratch, run resumes correctly.
4. answer_review_validation: rejecting approve on commitment/no-draft returns error.
5. edit_records_feedback: edited answer persists in FeedbackStore and appears in memory.
6. feedback_loop_proof: run 1 edit is retrieved in run 2, enabling autonomous answering.
7. final_signoff_rejection: rejecting FINAL_SIGNOFF sets rejected_by_human and halts before finalize_output.
8. approval_idempotency: duplicate resolve call returns current state cleanly.
9. concurrent_run_safety: multiple runs executed simultaneously without state bleed.
10. api_endpoints_e2e: TestClient exercising POST /runs, GET /approval, POST resolve, SSE stream, and report.
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import pytest
from fastapi.testclient import TestClient

from agent.llm import FakeLLM
from agent.memory import FeedbackStore
from agent.service import OperatorService, ValidationError
from agent.state import (
    ApprovalDecision,
    ApprovalItemDecision,
    ApprovalKind,
    ApprovalStatus,
    ItemAction,
    RunStatus,
)
from api.main import app, set_service


@pytest.fixture
def temp_env():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_runs.sqlite")
    fb_path = os.path.join(temp_dir, "test_feedback.json")
    # Copy policies into temp dir
    policies_dir = "data/policies"

    svc = OperatorService(
        db_path=db_path,
        feedback_path=fb_path,
        policies_dir=policies_dir,
        llm_client=FakeLLM(),
    )
    set_service(svc)
    yield svc, db_path, fb_path, temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_1_pause_at_interrupt(temp_env):
    svc, _, _, _ = temp_env
    run_state = svc.start_run("Acme security questionnaire, get it done.", run_id="run-pause-1")
    fut = svc._futures["run-pause-1"]
    fut.result(timeout=10)

    state = svc.get_run("run-pause-1")
    assert state is not None
    assert state.status == RunStatus.WAITING_APPROVAL
    assert state.pending_approval is not None
    assert state.pending_approval.kind == ApprovalKind.ANSWER_REVIEW
    assert len(state.pending_approval.items) > 0


def test_2_resume_with_approval(temp_env):
    svc, _, _, _ = temp_env
    svc.start_run("Acme security questionnaire, get it done.", run_id="run-resume-2")
    svc._futures["run-resume-2"].result(timeout=10)

    appr = svc.get_pending_approval("run-resume-2")
    assert appr is not None

    # Resolve answer review: edit all items with approved text
    item_decisions = [
        ApprovalItemDecision(question_id=it.question_id, action=ItemAction.EDIT, edited_answer="Approved vendor answer.")
        for it in appr.items
    ]
    dec = ApprovalDecision(approval_id=appr.id, decisions=item_decisions, decided_by="sec_admin")
    svc.resolve_approval("run-resume-2", appr.id, dec)
    svc._futures["run-resume-2"].result(timeout=10)

    # Next pause: FINAL_SIGNOFF
    state_after_ans = svc.get_run("run-resume-2")
    assert state_after_ans.status == RunStatus.WAITING_APPROVAL
    appr2 = svc.get_pending_approval("run-resume-2")
    assert appr2.kind == ApprovalKind.FINAL_SIGNOFF

    # Resolve final signoff
    dec2 = ApprovalDecision(approval_id=appr2.id, final_action=ItemAction.APPROVE, decided_by="ciso")
    svc.resolve_approval("run-resume-2", appr2.id, dec2)
    svc._futures["run-resume-2"].result(timeout=10)

    final_state = svc.get_run("run-resume-2")
    assert final_state.status == RunStatus.COMPLETED
    assert os.path.exists(final_state.artifacts["final_xlsx"])


def test_3_persistence_across_restart(temp_env):
    svc, db_path, fb_path, _ = temp_env
    svc.start_run("Acme security questionnaire", run_id="run-restart-3")
    svc._futures["run-restart-3"].result(timeout=10)

    # Completely instantiate a brand new OperatorService simulating new process
    new_svc = OperatorService(
        db_path=db_path,
        feedback_path=fb_path,
        policies_dir="data/policies",
        llm_client=FakeLLM(),
    )
    recovered_state = new_svc.get_run("run-restart-3")
    assert recovered_state is not None
    assert recovered_state.status == RunStatus.WAITING_APPROVAL
    appr = new_svc.get_pending_approval("run-restart-3")
    assert appr is not None

    # Resume from brand new process instance
    item_decisions = [
        ApprovalItemDecision(question_id=it.question_id, action=ItemAction.EDIT, edited_answer="Restarted process answer.")
        for it in appr.items
    ]
    dec = ApprovalDecision(approval_id=appr.id, decisions=item_decisions, decided_by="sec_admin")
    new_svc.resolve_approval("run-restart-3", appr.id, dec)
    new_svc._futures["run-restart-3"].result(timeout=10)

    appr2 = new_svc.get_pending_approval("run-restart-3")
    dec2 = ApprovalDecision(approval_id=appr2.id, final_action=ItemAction.APPROVE, decided_by="ciso")
    new_svc.resolve_approval("run-restart-3", appr2.id, dec2)
    new_svc._futures["run-restart-3"].result(timeout=10)

    resumed_final = new_svc.get_run("run-restart-3")
    assert resumed_final.status == RunStatus.COMPLETED


def test_4_answer_review_validation(temp_env):
    svc, _, _, _ = temp_env
    svc.start_run("Acme questionnaire", run_id="run-val-4")
    svc._futures["run-val-4"].result(timeout=10)

    appr = svc.get_pending_approval("run-val-4")
    commit_item = next(it for it in appr.items if "commitment" in (it.reason_for_escalation or "").lower() or "Q-34" in it.question_id)

    # Attempt to auto-approve commitment question -> must fail validation
    invalid_decisions = [
        ApprovalItemDecision(question_id=commit_item.question_id, action=ItemAction.APPROVE)
    ]
    dec = ApprovalDecision(approval_id=appr.id, decisions=invalid_decisions, decided_by="bad_approver")

    with pytest.raises(ValidationError) as exc:
        svc.resolve_approval("run-val-4", appr.id, dec)
    assert "Cannot approve commitment question" in str(exc.value)


def test_5_edit_records_feedback(temp_env):
    svc, _, fb_path, _ = temp_env
    svc.start_run("Acme questionnaire", run_id="run-fb-5")
    svc._futures["run-fb-5"].result(timeout=10)

    appr = svc.get_pending_approval("run-fb-5")
    q29_item = next(it for it in appr.items if it.question_id == "Q-29")

    custom_text = "Northwind Systems does not maintain FedRAMP High, but holds FedRAMP Moderate equivalence."
    dec = ApprovalDecision(
        approval_id=appr.id,
        decisions=[
            ApprovalItemDecision(
                question_id="Q-29",
                action=ItemAction.EDIT,
                edited_answer=custom_text,
                note="Verified with compliance director",
            )
        ],
        decided_by="ciso_auditor",
    )
    svc.resolve_approval("run-fb-5", appr.id, dec)

    # Check feedback store on disk
    rules = svc.feedback_store.load_rules()
    matching_rule = next((r for r in rules if r.question == q29_item.question_text), None)
    assert matching_rule is not None
    assert matching_rule.answer == custom_text
    assert matching_rule.approver == "ciso_auditor"


def test_6_feedback_loop_proof(temp_env):
    """
    Run 1 has a human edit.
    Run 2 retrieves that human edit rule autonomously without human edit.
    """
    svc, _, _, _ = temp_env
    # Run 1: Human teaches agent about FedRAMP
    svc.start_run("Acme questionnaire", run_id="run-teach-1")
    svc._futures["run-teach-1"].result(timeout=10)
    appr1 = svc.get_pending_approval("run-teach-1")

    fedramp_text = "Northwind Systems does not have FedRAMP High but holds SOC2 Type II."
    svc.resolve_approval(
        "run-teach-1",
        appr1.id,
        ApprovalDecision(
            approval_id=appr1.id,
            decisions=[
                ApprovalItemDecision(
                    question_id="Q-29",
                    action=ItemAction.EDIT,
                    edited_answer=fedramp_text,
                )
            ],
            decided_by="human_expert",
        ),
    )

    # Run 2: Fresh run
    svc.start_run("Acme questionnaire", run_id="run-autonomous-2")
    svc._futures["run-autonomous-2"].result(timeout=10)

    state2 = svc.get_run("run-autonomous-2")
    q29_ans = next(a for a in state2.answers if a.question_id == "Q-29")
    # Proves Run 2 answered Q-29 using feedback store
    assert q29_ans.decision.value == "answered"
    assert fedramp_text in q29_ans.text
    assert "feedback_rules.json" in [c.doc for c in q29_ans.citations]


def test_7_final_signoff_rejection(temp_env):
    svc, _, _, _ = temp_env
    svc.start_run("Acme questionnaire", run_id="run-reject-7")
    svc._futures["run-reject-7"].result(timeout=10)

    appr1 = svc.get_pending_approval("run-reject-7")
    svc.resolve_approval(
        "run-reject-7",
        appr1.id,
        ApprovalDecision(
            approval_id=appr1.id,
            decisions=[
                ApprovalItemDecision(question_id=it.question_id, action=ItemAction.EDIT, edited_answer="Approved")
                for it in appr1.items
            ],
            decided_by="admin",
        ),
    )
    svc._futures["run-reject-7"].result(timeout=10)

    appr2 = svc.get_pending_approval("run-reject-7")
    assert appr2.kind == ApprovalKind.FINAL_SIGNOFF

    # Human REJECTS final sign-off
    dec_reject = ApprovalDecision(
        approval_id=appr2.id,
        final_action=ItemAction.REJECT,
        decided_by="lead_ciso",
    )
    svc.resolve_approval("run-reject-7", appr2.id, dec_reject)
    svc._futures["run-reject-7"].result(timeout=10)

    final_state = svc.get_run("run-reject-7")
    assert final_state.status == RunStatus.REJECTED_BY_HUMAN
    assert "final_xlsx" not in final_state.artifacts


def test_8_approval_idempotency(temp_env):
    svc, _, _, _ = temp_env
    svc.start_run("Acme questionnaire", run_id="run-idem-8")
    svc._futures["run-idem-8"].result(timeout=10)

    appr = svc.get_pending_approval("run-idem-8")
    dec = ApprovalDecision(
        approval_id=appr.id,
        decisions=[
            ApprovalItemDecision(question_id=it.question_id, action=ItemAction.EDIT, edited_answer="Answer")
            for it in appr.items
        ],
        decided_by="admin",
    )
    # First resolve
    s1 = svc.resolve_approval("run-idem-8", appr.id, dec)
    svc._futures["run-idem-8"].result(timeout=10)

    # Second resolve with same decision (duplicate network call)
    s2 = svc.resolve_approval("run-idem-8", appr.id, dec)
    assert s2 is not None


def test_9_concurrent_run_safety(temp_env):
    svc, _, _, _ = temp_env
    # Start 3 runs simultaneously
    s1 = svc.start_run("Acme run 1", run_id="run-conc-1")
    s2 = svc.start_run("Acme run 2", run_id="run-conc-2")
    s3 = svc.start_run("Acme run 3", run_id="run-conc-3")

    svc._futures["run-conc-1"].result(timeout=15)
    svc._futures["run-conc-2"].result(timeout=15)
    svc._futures["run-conc-3"].result(timeout=15)

    r1 = svc.get_run("run-conc-1")
    r2 = svc.get_run("run-conc-2")
    r3 = svc.get_run("run-conc-3")

    assert r1.run_id == "run-conc-1"
    assert r2.run_id == "run-conc-2"
    assert r3.run_id == "run-conc-3"
    assert r1.status == RunStatus.WAITING_APPROVAL
    assert r2.status == RunStatus.WAITING_APPROVAL
    assert r3.status == RunStatus.WAITING_APPROVAL


def test_10_api_endpoints_e2e(temp_env):
    svc, _, _, _ = temp_env
    client = TestClient(app)

    # Health
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # POST /runs
    create_resp = client.post("/runs", json={"request_text": "Acme questionnaire", "run_id": "run-api-10"})
    assert create_resp.status_code == 202
    assert create_resp.json()["run_id"] == "run-api-10"

    # Wait for pause
    svc._futures["run-api-10"].result(timeout=10)

    # GET /runs/run-api-10
    get_resp = client.get("/runs/run-api-10")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "waiting_approval"

    # GET /runs/run-api-10/approval
    appr_resp = client.get("/runs/run-api-10/approval")
    assert appr_resp.status_code == 200
    appr_data = appr_resp.json()
    assert appr_data["kind"] == "ANSWER_REVIEW"

    # POST /runs/run-api-10/approvals/{aid}
    items_dec = [
        {"question_id": it["question_id"], "action": "edit", "edited_answer": "API approved answer."}
        for it in appr_data["items"]
    ]
    resolve_resp = client.post(
        f"/runs/run-api-10/approvals/{appr_data['id']}",
        json={"approval_id": appr_data["id"], "decisions": items_dec, "decided_by": "api_client"},
    )
    assert resolve_resp.status_code == 200

    # Wait for FINAL_SIGNOFF
    svc._futures["run-api-10"].result(timeout=10)
    appr2_resp = client.get("/runs/run-api-10/approval")
    assert appr2_resp.status_code == 200
    appr2_data = appr2_resp.json()
    assert appr2_data["kind"] == "FINAL_SIGNOFF"

    # Approve FINAL_SIGNOFF
    client.post(
        f"/runs/run-api-10/approvals/{appr2_data['id']}",
        json={"approval_id": appr2_data["id"], "final_action": "approve", "decided_by": "api_client"},
    )
    svc._futures["run-api-10"].result(timeout=10)

    # GET /runs/run-api-10/events
    events_resp = client.get("/runs/run-api-10/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json()) >= 8

    # GET /runs/run-api-10/report
    report_resp = client.get("/runs/run-api-10/report")
    assert report_resp.status_code == 200
    assert "report_md" in report_resp.json()

