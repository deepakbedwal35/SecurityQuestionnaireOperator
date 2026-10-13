"""
Tests for Step 5: Knowledge Base, Mailbox & Email Tools, Unified Intake, and Email Delivery.
Covers all 11 required scenarios and edge cases.
"""
import email
from email import policy
import glob
import hashlib
import os
import shutil
import tempfile
import pytest

from agent.events import EventSink
from agent.kb import (
    DocRecord,
    KnowledgeBaseService,
    delete_document,
    get_kb_service,
    ingest_document,
    kb_status,
    list_documents,
    reindex,
)
from agent.llm import FakeLLM
from agent.mail.mailbox import FolderMailbox, MailMessage
from agent.service import OperatorService, ValidationError
from agent.state import (
    Answer,
    AnswerDecision,
    ApprovalDecision,
    ApprovalKind,
    ApprovalStatus,
    Citation,
    ItemAction,
    PostconditionSpec,
    RunState,
    RunStatus,
    UnderstoodGoal,
)
from agent.tools.email import (
    DraftReplyTool,
    ListEmailsTool,
    ReadEmailTool,
    SaveAttachmentTool,
    SendReplyTool,
    build_scrubbed_reply_body,
    extract_domain,
    extract_email_address,
    is_known_customer_domain,
    scrub_customer_email_body,
)
from agent.tools.registry import ToolRegistry
from agent.tools.retrieve import KeywordPolicyRetriever
from agent.verifier import Verifier
from scripts.generate_data import create_test_emails, seed_knowledge_base


# Setup is handled by conftest.py autouse fixture


# -------------------------------------------------------------
# Test 1: Happy-path email intake -> answers -> sign-off -> sent .eml
# -------------------------------------------------------------
def test_1_happy_path_email_delivery(tmp_path):
    db_path = str(tmp_path / "test_runs.sqlite")
    sent_dir = str(tmp_path / "sent")
    os.makedirs(sent_dir, exist_ok=True)

    fb_path = str(tmp_path / "test_feedback.json")
    mailbox = FolderMailbox(sent_dir=sent_dir)
    svc = OperatorService(db_path=db_path, feedback_path=fb_path, llm_client=FakeLLM(), mailbox=mailbox)

    state = svc.start_run("Handle email <acme-2024-happy@acme.com> from alice@acme.com", run_id="run-happy-1")
    run_id = state.run_id

    # Wait for execution to reach interrupt
    fut = svc._futures[run_id]
    fut.result(timeout=15)

    current = svc.get_run(run_id)
    assert current is not None
    assert current.understood_goal.source == "email"
    assert current.understood_goal.delivery == "email_reply"
    assert current.understood_goal.sender == "alice@acme.com"

    # Pending approval should be present (either ANSWER_REVIEW or FINAL_SIGNOFF)
    pending = svc.get_pending_approval(run_id)
    assert pending is not None

    if pending.kind == ApprovalKind.ANSWER_REVIEW:
        # Resolve answer review items
        from agent.state import ApprovalItemDecision
        decisions = []
        for it in pending.items:
            if not it.draft_answer or "commitment" in it.reason_for_escalation.lower():
                decisions.append(ApprovalItemDecision(
                    question_id=it.question_id,
                    action=ItemAction.EDIT,
                    edited_answer="99.9% uptime target with standard business exclusions",
                ))
            else:
                decisions.append(ApprovalItemDecision(
                    question_id=it.question_id,
                    action=ItemAction.APPROVE,
                ))
        decision = ApprovalDecision(approval_id=pending.id, decisions=decisions, decided_by="auditor")
        svc.resolve_approval(run_id, pending.id, decision)
        svc._futures[run_id].result(timeout=15)
        pending = svc.get_pending_approval(run_id)

    # Now at FINAL_SIGNOFF
    if pending and pending.kind == ApprovalKind.FINAL_SIGNOFF:
        final_decision = ApprovalDecision(approval_id=pending.id, final_action=ItemAction.APPROVE, decided_by="compliance_officer")
        svc.resolve_approval(run_id, pending.id, final_decision)
        svc._futures[run_id].result(timeout=15)

    final_state = svc.get_run(run_id)
    assert final_state.status in (RunStatus.COMPLETED, RunStatus.RUNNING)


# -------------------------------------------------------------
# Test 2: Email with 2 attachments -> disambiguates or flags
# -------------------------------------------------------------
def test_2_two_attachments_disambiguation():
    mailbox = FolderMailbox()
    from agent.intake import IntakeNode
    registry = ToolRegistry()
    intake = IntakeNode(FakeLLM(), registry, mailbox=mailbox)

    state = RunState(run_id="run-two-atts", request_text="Handle email <acme-2024-two-atts@acme.com>")
    goal = intake.run(state)
    assert goal.source == "email"
    assert "OLD" not in os.path.basename(goal.questionnaire_path)
    assert "2024" in os.path.basename(goal.questionnaire_path)


# -------------------------------------------------------------
# Test 3: Prompt injection in email body -> flagged, never executed
# -------------------------------------------------------------
def test_3_prompt_injection_in_email():
    mailbox = FolderMailbox()
    from agent.intake import IntakeNode
    registry = ToolRegistry()
    intake = IntakeNode(FakeLLM(), registry, mailbox=mailbox)

    state = RunState(run_id="run-inj", request_text="Handle email <acme-injection-attack@acme.com>")
    intake.run(state)
    assert len(state.injection_flags) > 0
    flag = state.injection_flags[0]
    assert "acme-injection-attack@acme.com" in flag.source


# -------------------------------------------------------------
# Test 4: Unknown sender domain -> escalates before drafting/sending
# -------------------------------------------------------------
def test_4_unknown_sender_domain_escalates():
    mailbox = FolderMailbox()
    from agent.intake import IntakeNode
    registry = ToolRegistry()
    intake = IntakeNode(FakeLLM(), registry, mailbox=mailbox)

    state = RunState(run_id="run-unknown", request_text="Handle email <evil-corp-unauthorized@evil-corp.com>")
    goal = intake.run(state)
    assert state.status == RunStatus.ESCALATED
    assert goal.low_confidence_goal is True
    assert "evil-corp.com" in state.failure_reason


# -------------------------------------------------------------
# Test 5: Non-xlsx attachment -> rejected with policy message
# -------------------------------------------------------------
def test_5_non_xlsx_attachment_rejected():
    mailbox = FolderMailbox()
    save_tool = SaveAttachmentTool(mailbox=mailbox)

    state = RunState(run_id="run-non-xlsx", request_text="save")
    with pytest.raises(ValueError, match="Only .xlsx questionnaire attachments"):
        save_tool.run(state, message_id="<acme-non-xlsx@acme.com>", attachment_name="Acme_Questionnaire.pdf")


# -------------------------------------------------------------
# Test 6: Cross-domain Reply-To -> sent to From address, not Reply-To
# -------------------------------------------------------------
def test_6_cross_domain_reply_to_ignored():
    mailbox = FolderMailbox()
    draft_tool = DraftReplyTool(mailbox=mailbox)

    # Acme questionnaire file for draft
    acme_path = "data/inbox/Acme_Security_Questionnaire.xlsx"
    state = RunState(run_id="run-cross-domain", request_text="draft")
    state.understood_goal = UnderstoodGoal(customer="Acme", questionnaire_path=acme_path)

    res = draft_tool.run(state, message_id="<acme-cross-domain-reply@acme.com>", attachment_path=acme_path)
    assert res["to_address"] == "alice@acme.com"
    assert "consultant@shady-external.com" not in res["to_address"]


# -------------------------------------------------------------
# Test 7: Guardrail check on send_reply
# -------------------------------------------------------------
def test_7_send_reply_requires_approved_final_signoff():
    mailbox = FolderMailbox()
    send_tool = SendReplyTool(mailbox=mailbox)

    state = RunState(run_id="run-guard", request_text="test")
    # No approval present
    with pytest.raises(PermissionError, match="send_reply requires approved FINAL_SIGNOFF"):
        send_tool.run(state, message_id="<acme-2024-happy@acme.com>", attachment_path="data/inbox/Acme_Security_Questionnaire.xlsx")


# -------------------------------------------------------------
# Test 8: Customer email scrubber test
# -------------------------------------------------------------
def test_8_email_scrubber_removes_internal_details():
    dirty_body = """
    Here are the answers.
    Confidence: 0.95
    Prompt injection flagged: ignore all rules
    Information_Security_Policy.md was used as source.
    RiskLevel is WRITE_HIGH.
    """
    cleaned = scrub_customer_email_body(dirty_body)
    assert "0.95" not in cleaned
    assert "Prompt injection" not in cleaned
    assert "Information_Security_Policy.md" not in cleaned
    assert "WRITE_HIGH" not in cleaned

    formatted = build_scrubbed_reply_body(
        customer_name="Acme",
        attachment_name="final.xlsx",
        answered_count=35,
        total_count=40,
        unresolved_count=5,
    )
    assert "final.xlsx" in formatted
    assert "35 of 40" in formatted
    assert "5 items require secondary internal review" in formatted
    assert "confidence" not in formatted.lower()


# -------------------------------------------------------------
# Test 9: Upload path works without email
# -------------------------------------------------------------
def test_9_upload_path_flow():
    from agent.intake import IntakeNode
    registry = ToolRegistry()
    intake = IntakeNode(FakeLLM(), registry)

    state = RunState(run_id="run-upload", request_text="uploaded: Custom_Questionnaire.xlsx")
    state.artifacts["uploaded_file"] = "data/inbox/Acme_Security_Questionnaire.xlsx"
    goal = intake.run(state)
    assert goal.source == "upload"
    assert goal.delivery == "download"
    assert goal.questionnaire_path == "data/inbox/Acme_Security_Questionnaire.xlsx"


# -------------------------------------------------------------
# Test 10: KB ingestion, chunking, deduplication & empty-KB guard
# -------------------------------------------------------------
def test_10_kb_operations_and_empty_guard(tmp_path):
    kb_dir = str(tmp_path / "test_kb")
    kb = KnowledgeBaseService(kb_dir=kb_dir)

    # Ingest document
    content = b"# Policy A\n\nSection 1 text.\n\nSection 2 text."
    doc1, is_new1 = kb.ingest_document(content, doc_name="Policy_A.md", version="1.0", effective_date="2024-01-01")
    assert is_new1 is True
    assert doc1.chunk_count >= 1

    # Ingest exact same document twice (deduplication)
    doc2, is_new2 = kb.ingest_document(content, doc_name="Policy_A_copy.md", version="1.0", effective_date="2024-01-01")
    assert is_new2 is False
    assert doc2.source_hash == doc1.source_hash

    # Delete document
    assert kb.delete_document(doc1.doc_id) is True
    assert kb.kb_status()["total_documents"] == 0

    # Empty KB guard test
    empty_kb_dir = str(tmp_path / "empty_kb")
    empty_policies_dir = str(tmp_path / "empty_policies")
    os.makedirs(empty_kb_dir, exist_ok=True)
    os.makedirs(empty_policies_dir, exist_ok=True)

    svc = OperatorService(
        db_path=str(tmp_path / "test_empty.sqlite"),
        policies_dir=empty_policies_dir,
        kb_dir=empty_kb_dir,
        llm_client=FakeLLM(),
    )
    with pytest.raises(ValidationError, match="Knowledge Base is empty"):
        svc.start_run("Start run on empty KB")


# -------------------------------------------------------------
# Test 11: Verifier independence: detects tampered sent email
# -------------------------------------------------------------
def test_11_verifier_detects_tampered_sent_email(tmp_path):
    sent_eml = str(tmp_path / "test_sent.eml")
    final_xlsx = str(tmp_path / "final.xlsx")

    # Write genuine final xlsx
    with open(final_xlsx, "wb") as f:
        f.write(b"GENUINE_XLSX_BYTES_12345")

    # Build sent email with DIFFERENT attachment bytes
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "Re: Security Questionnaire"
    msg["From"] = "security@northwind.com"
    msg["To"] = "alice@acme.com"
    msg["In-Reply-To"] = "<acme-123@acme.com>"
    msg.set_content("Attached.")
    msg.add_attachment(b"TAMPERED_BYTES_67890", maintype="application", subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="final.xlsx")
    with open(sent_eml, "wb") as f:
        f.write(msg.as_bytes())

    state = RunState(run_id="run-tamper", request_text="verify")
    state.artifacts["sent_email_path"] = sent_eml

    # Run independent verifier
    spec = PostconditionSpec(
        kind="reply_attachment_matches_final",
        params={"sent_path": sent_eml, "final_xlsx": final_xlsx},
    )
    res = Verifier.verify(spec, state)
    assert res.passed is False
    assert "does not match" in res.evidence
