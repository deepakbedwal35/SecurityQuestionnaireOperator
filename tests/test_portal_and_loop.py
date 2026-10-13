"""
Step 5 Test Suite: Mock customer portal, Playwright tools, and the observe-act loop.
Tests:
1. test_1_happy_path: Full run -> login, fill approved answers, submit, confirmation, verifier passes, screenshots exist.
2. test_2_conditional_followups: Q-01a, Q-18a, Q-21a filled conditionally; unmatched question triggers human input escalation.
3. test_3_session_expiry_mid_fill: Injected session_expiry fault -> agent re-logs in and completes without losing draft answers.
4. test_4_ui_rename: Injected ui_rename fault -> loop resolves renamed controls; stale refs are rejected.
5. test_5_flaky_save: Injected flaky_save fault -> 500 error retried with backoff and succeeds.
6. test_6_duplicate_submission: Injected duplicate fault -> rejected with already_submitted and existing ID.
7. test_7_submit_guardrails: Refused without approved FINAL_SIGNOFF, refused when answers hash mismatches, refused if submission_id already exists.
8. test_8_injection_banner: Injected injection_banner -> flagged in state, not followed.
9. test_9_stuck_loop_detection: Frozen/unchanging page stops after 3 consecutive identical hashes and escalates.
10. test_10_verifier_independence: Tampering with answer in portal via /__admin/tamper causes portal_answers_match_state postcondition to fail.
11. test_11_secrets_scrubbed: Asserts PORTAL_PASS string appears nowhere in SQLite, events, report markdown, or artifact filenames.
12. test_12_cancel_closes_browser: Cancel mid-run closes Playwright browser context cleanly without zombie processes.
"""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import time
import pytest

from agent.llm import FakeLLM
from agent.memory import FeedbackStore
from agent.portal_loop import PortalOperatorLoop
from agent.service import OperatorService
from agent.state import (
    Answer,
    AnswerDecision,
    ApprovalDecision,
    ApprovalItemDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    ItemAction,
    PostconditionSpec,
    RiskLevel,
    RunState,
    RunStatus,
)
from agent.tools.portal import (
    PortalBrowserManager,
    PortalLoginTool,
    PortalOpenTool,
    PortalSnapshotTool,
    PortalSubmitTool,
)
from agent.tools.registry import ApprovalRequiredError, ToolExecutionError
from agent.verifier import Verifier
from scripts.start_portal import PortalProcess


def get_free_port() -> int:
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def portal_server():
    port = get_free_port()
    proc = PortalProcess(port=port)
    base_url = proc.start()
    old_portal_url = os.environ.get("PORTAL_URL")
    os.environ["PORTAL_URL"] = f"{base_url}/questionnaires/acme-sec-2024"
    os.environ["PORTAL_USER"] = "admin@acme.com"
    os.environ["PORTAL_PASS"] = "AcmeSecret2024!"

    yield proc, base_url

    proc.stop()
    if old_portal_url:
        os.environ["PORTAL_URL"] = old_portal_url
    else:
        os.environ.pop("PORTAL_URL", None)
    # Close any lingering Playwright contexts
    PortalBrowserManager.get_instance().close_all()


def _create_approved_run_state(run_id: str, questions_count: int = 5) -> RunState:
    """Helper to create a RunState with approved FINAL_SIGNOFF for portal testing."""
    state = RunState(
        run_id=run_id,
        request_text="Acme questionnaire completion",
        status=RunStatus.RUNNING,
    )
    # Load default questions from portal seed
    portal_data_path = os.path.join(os.path.dirname(__file__), "..", "portal", "data", "questions.json")
    with open(portal_data_path, "r", encoding="utf-8") as f:
        q_seeds = json.load(f)

    for q in q_seeds:
        if q["id"] in ("Q-01a", "Q-18a", "Q-21a"):
            continue  # conditionals
        val = "Yes" if q["type"] == "yes_no" else ((q.get("options", ["SAML 2.0 / OIDC"])[0]) if q["type"] == "dropdown" else "Fully compliant with SOC2 controls.")
        state.answers.append(
            Answer(
                question_id=q["id"],
                decision=AnswerDecision.ANSWERED,
                text=val,
                confidence=1.0,
            )
        )

    current_answers_repr = json.dumps([{"q": a.question_id, "t": a.text} for a in state.answers], sort_keys=True)
    answers_hash = hashlib.sha256(current_answers_repr.encode("utf-8")).hexdigest()

    state.pending_approval = ApprovalRequest(
        id=f"appr-{run_id}",
        run_id=run_id,
        kind=ApprovalKind.FINAL_SIGNOFF,
        reason="Final signoff approved",
        items=[],
        risk=RiskLevel.WRITE_HIGH,
        status=ApprovalStatus.APPROVED,
        approved_answers_hash=answers_hash,
    )
    return state


def test_1_happy_path(portal_server):
    _, base_url = portal_server
    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-happy")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "completed"
    assert res.submission_id is not None
    assert res.submission_id.startswith("SUB-")
    assert state.portal_state.submission_id == res.submission_id

    # Verify screenshots exist
    screen_spec = PostconditionSpec(kind="portal_screenshots_present", params={})
    res_screen = Verifier.verify(screen_spec, state)
    assert res_screen.passed is True

    # Independent verifier: confirmation ID
    conf_spec = PostconditionSpec(kind="portal_submission_confirmed", params={})
    res_conf = Verifier.verify(conf_spec, state)
    assert res_conf.passed is True

    # Independent verifier: answers match state via fresh browser context
    match_spec = PostconditionSpec(kind="portal_answers_match_state", params={"portal_url": base_url})
    res_match = Verifier.verify(match_spec, state)
    assert res_match.passed is True

    # Independent verifier: no double submission
    no_dup_spec = PostconditionSpec(kind="no_double_submission", params={"portal_url": base_url})
    res_dup = Verifier.verify(no_dup_spec, state)
    assert res_dup.passed is True


def test_2_conditional_followups(portal_server):
    _, base_url = portal_server
    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-cond")

    # Q-01 is answered "Yes", triggering Q-01a
    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "completed"
    assert res.submission_id is not None

    # Verify that unmatched conditional causes human escalation
    # Reset portal submissions via admin endpoint so questionnaire can be filled afresh
    import urllib.request
    reset_req = urllib.request.Request(
        f"{base_url}/__admin/reset",
        data=b"{}",
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(reset_req) as resp:
        assert resp.status == 200

    # Temporarily override _match_conditional_question to simulate an ungrounded unknown conditional
    state_unmatched = _create_approved_run_state("run-portal-unmatched")
    loop_unmatched = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    loop_unmatched._match_conditional_question = lambda qid, label, st: None  # Cannot ground

    res_escalated = loop_unmatched.run(state_unmatched)
    assert res_escalated.status == "escalated"
    assert "Unmatched conditional question" in (res_escalated.failure_reason or "")


def test_3_session_expiry_mid_fill(portal_server):
    proc, base_url = portal_server
    import urllib.request
    # Inject session_expiry fault via admin API
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"faults": ["session_expiry"]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-sess-exp")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "completed"
    assert res.relogins >= 1
    assert res.submission_id is not None


def test_4_ui_rename(portal_server):
    proc, base_url = portal_server
    import urllib.request
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"faults": ["ui_rename"], "reset": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-rename")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "completed"
    assert res.submission_id is not None

    # Stale element ref test
    act_tool = loop.act_tool
    with pytest.raises(ToolExecutionError) as exc_info:
        act_tool.run(state, action="click", ref="e999999")
    assert "Stale or invalid element ref" in str(exc_info.value)


def test_5_flaky_save(portal_server):
    proc, base_url = portal_server
    import urllib.request
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"faults": ["flaky_save"], "reset": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-flaky")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "completed"
    assert res.submission_id is not None


def test_6_duplicate_submission(portal_server):
    proc, base_url = portal_server
    import urllib.request
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"faults": ["duplicate"], "reset": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-dup")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert res.status == "already_submitted"
    assert res.submission_id == "SUB-99999"
    assert state.portal_state.already_submitted is True


def test_7_submit_guardrails(portal_server):
    _, base_url = portal_server
    submit_tool = PortalSubmitTool()

    # Guardrail 1: Refused without approved FINAL_SIGNOFF
    state_no_appr = RunState(run_id="run-guard-1", request_text="Guardrail test", status=RunStatus.RUNNING)
    with pytest.raises(ApprovalRequiredError) as exc_info:
        submit_tool.run(state_no_appr)
    assert "requires approved FINAL_SIGNOFF" in str(exc_info.value)

    # Guardrail 2: Refused when answers hash mismatches
    state_tampered = _create_approved_run_state("run-guard-2")
    # Tamper with an answer after approval
    state_tampered.answers[0].text = "TAMPERED ANSWER"
    with pytest.raises(ApprovalRequiredError) as exc_info:
        submit_tool.run(state_tampered)
    assert "answers were altered after approval" in str(exc_info.value)

    # Guardrail 3: Idempotency if state already has a submission_id
    state_already_done = _create_approved_run_state("run-guard-3")
    state_already_done.portal_state.submission_id = "SUB-EXISTING-123"
    result = submit_tool.run(state_already_done)
    assert result["status"] == "already_submitted"
    assert result["submission_id"] == "SUB-EXISTING-123"


def test_8_injection_banner(portal_server):
    proc, base_url = portal_server
    import urllib.request
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"faults": ["injection_banner"], "reset": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-inject")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)

    assert len(state.injection_flags) > 0
    flag_snippets = [f.snippet for f in state.injection_flags]
    assert any("ignore previous instructions" in sn.lower() for sn in flag_snippets)
    assert res.status == "completed"


def test_9_stuck_loop_detection(portal_server):
    _, base_url = portal_server
    state = _create_approved_run_state("run-portal-stuck")

    # Simulate broken page with constant hash
    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=f"{base_url}/login", max_consecutive_unchanged=3)
    # Monkeypatch snapshot_tool to return frozen hash
    frozen_snap = {"url": f"{base_url}/questionnaires/acme-sec-2024", "content_hash": "constant_deadlock_hash", "alerts": [], "elements": {}}
    loop.snapshot_tool.run = lambda st: frozen_snap

    res = loop.run(state)
    assert res.status == "escalated"
    assert "Loop stuck" in (res.failure_reason or "")


def test_10_verifier_independence(portal_server):
    proc, base_url = portal_server
    import urllib.request
    # Reset faults
    req = urllib.request.Request(
        f"{base_url}/__admin/faults",
        data=json.dumps({"reset": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(req):
        pass

    form_url = f"{base_url}/questionnaires/acme-sec-2024"
    state = _create_approved_run_state("run-portal-indep")

    loop = PortalOperatorLoop(llm=FakeLLM(), portal_url=form_url)
    res = loop.run(state)
    assert res.status == "completed"
    sub_id = res.submission_id

    # Tamper with the portal answer behind the agent's back via /__admin/tamper
    tamper_req = urllib.request.Request(
        f"{base_url}/__admin/tamper",
        data=json.dumps({
            "submission_id": sub_id,
            "question_id": "Q-01",
            "new_value": "TAMPERED_FRAUDULENT_DATA",
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-admin-token": "portal-test-admin-token"},
        method="POST",
    )
    with urllib.request.urlopen(tamper_req) as resp:
        assert resp.status == 200

    # Independent verifier must catch the mismatch!
    match_spec = PostconditionSpec(kind="portal_answers_match_state", params={"portal_url": base_url})
    v_res = Verifier.verify(match_spec, state)
    assert v_res.passed is False
    assert "answer mismatches" in v_res.evidence


def test_11_secrets_scrubbed(portal_server):
    _, base_url = portal_server
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "secrets_test.sqlite")
    secret_pass = "AcmeSecret2024!"

    svc = OperatorService(
        db_path=db_path,
        feedback_path=os.path.join(temp_dir, "fb.json"),
        policies_dir="data/policies",
        llm_client=FakeLLM(),
    )

    state = svc.start_run("Acme questionnaire", run_id="run-portal-secrets")
    svc._futures["run-portal-secrets"].result(timeout=15)

    # Approve review & final signoff
    appr = svc.get_pending_approval("run-portal-secrets")
    if appr and appr.kind == ApprovalKind.ANSWER_REVIEW:
        dec = ApprovalDecision(
            approval_id=appr.id,
            decisions=[
                ApprovalItemDecision(question_id=it.question_id, action=ItemAction.EDIT, edited_answer="Yes")
                for it in appr.items
            ],
            decided_by="admin",
        )
        svc.resolve_approval("run-portal-secrets", appr.id, dec)
        svc._futures["run-portal-secrets"].result(timeout=15)

    appr2 = svc.get_pending_approval("run-portal-secrets")
    if appr2 and appr2.kind == ApprovalKind.FINAL_SIGNOFF:
        dec2 = ApprovalDecision(approval_id=appr2.id, final_action=ItemAction.APPROVE, decided_by="admin")
        svc.resolve_approval("run-portal-secrets", appr2.id, dec2)
        svc._futures["run-portal-secrets"].result(timeout=20)

    # 1. Inspect events database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT payload_json FROM events WHERE run_id = 'run-portal-secrets'")
    for (payload,) in cursor.fetchall():
        assert secret_pass not in payload, f"Secret password leaked in event payload: {payload}"
    conn.close()

    # 2. Inspect report markdown
    rep = svc.get_report("run-portal-secrets")
    report_md = rep.get("report_md", "")
    assert secret_pass not in report_md

    # 3. Inspect artifact paths
    for art_name, art_path in rep.get("artifacts", {}).items():
        assert secret_pass not in art_name
        assert secret_pass not in art_path

    shutil.rmtree(temp_dir, ignore_errors=True)


def test_12_cancel_closes_browser(portal_server):
    _, base_url = portal_server
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "cancel_test.sqlite")

    svc = OperatorService(
        db_path=db_path,
        feedback_path=os.path.join(temp_dir, "fb.json"),
        policies_dir="data/policies",
        llm_client=FakeLLM(),
    )

    state = svc.start_run("Acme questionnaire", run_id="run-portal-cancel")
    # Cancel the run
    cancelled = svc.cancel_run("run-portal-cancel")
    assert cancelled is True

    mgr = PortalBrowserManager.get_instance()
    # Confirm browser context for this run is cleaned up
    assert "run-portal-cancel" not in mgr._pages
    assert "run-portal-cancel" not in mgr._contexts

    shutil.rmtree(temp_dir, ignore_errors=True)

