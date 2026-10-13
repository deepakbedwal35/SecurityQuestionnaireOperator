"""
Email tools package: list_emails, read_email, save_attachment, draft_reply, send_reply.
Includes strict outgoing email body scrubber and domain authorization guards.
"""
from datetime import datetime, timezone
import email
from email import policy
from email.message import EmailMessage
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.mail.mailbox import FolderMailbox, Mailbox, MailMessage
from agent.policy import PolicyEngine
from agent.state import AnswerDecision, ApprovalKind, ApprovalStatus, RiskLevel, RunState
from agent.tools.registry import Tool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_email_address(raw_header: str) -> str:
    match = re.search(r"[\w\.-]+@[\w\.-]+", raw_header)
    return match.group(0).lower() if match else raw_header.strip().lower()


def extract_domain(address_or_header: str) -> str:
    addr = extract_email_address(address_or_header)
    if "@" in addr:
        return addr.split("@")[-1].lower()
    return ""


def load_known_customers(config_path: Optional[str] = None) -> Dict[str, List[str]]:
    from agent.config import get_known_customers_file
    path = config_path or get_known_customers_file()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def is_known_customer_domain(sender_email: str, config_path: Optional[str] = None) -> bool:
    from agent.config import get_known_customers_file
    path = config_path or get_known_customers_file()
    domain = extract_domain(sender_email)
    known = load_known_customers(path)
    for cust, domains in known.items():
        if domain in [d.lower() for d in domains]:
            return True
    return False


def scrub_customer_email_body(raw_body: str) -> str:
    """
    Guarantees outgoing email body never leaks internal reasoning, confidence scores,
    flags, or internal policy filenames.
    """
    forbidden_patterns = [
        r"(?i)confidence[:\s]+[0-9\.]+",
        r"(?i)confidence score",
        r"(?i)prompt injection",
        r"(?i)injection flag",
        r"(?i)flagged",
        r"(?i)internal policy",
        r"(?i)Information_Security_Policy\.md",
        r"(?i)Data_Retention_Policy\.md",
        r"(?i)Incident_Response_Plan\.md",
        r"(?i)Access_Control_Policy\.md",
        r"(?i)feedback_rules\.json",
        r"(?i)PolicyEngine",
        r"(?i)RiskLevel",
        r"(?i)WRITE_HIGH",
        r"(?i)WRITE_LOW",
    ]
    cleaned = raw_body
    for pat in forbidden_patterns:
        cleaned = re.sub(pat, "[redacted]", cleaned)
    return cleaned


def build_scrubbed_reply_body(
    customer_name: str,
    attachment_name: str,
    answered_count: int,
    total_count: int,
    unresolved_count: int = 0,
) -> str:
    """
    Fixed template + minimal customer summary.
    Strips all internal reasoning, confidence scores, flags, and policy filenames.
    """
    lines = [
        f"Hello,",
        "",
        f"Thank you for contacting Northwind Systems. We have completed our review of your security questionnaire.",
        "",
        f"Summary of completed review:",
        f"- Attached file: {attachment_name}",
        f"- Questions answered: {answered_count} of {total_count}",
    ]
    if unresolved_count > 0:
        lines.append(f"- Note on pending items: {unresolved_count} items require secondary internal review and will be provided separately.")

    lines.extend([
        "",
        "Please find the completed questionnaire attached to this email.",
        "",
        "Best regards,",
        "Northwind Systems Security & Compliance Team",
    ])
    body = "\n".join(lines)
    return scrub_customer_email_body(body)


# --- Tool 1: list_emails ---

class ListEmailsInput(BaseModel):
    limit: int = 10


class ListEmailsOutput(BaseModel):
    emails: List[Dict[str, Any]]


class ListEmailsTool(Tool):
    name = "list_emails"
    description = "Lists unhandled incoming emails from the customer inbox."
    risk = RiskLevel.READ
    args_schema = ListEmailsInput
    output_schema = ListEmailsOutput

    def __init__(self, mailbox: Optional[Mailbox] = None) -> None:
        self.mailbox = mailbox or FolderMailbox()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        limit = kwargs.get("limit", 10)
        messages = self.mailbox.list_emails()[:limit]
        out_list = []
        for m in messages:
            att_info = [{"filename": a.filename, "size": a.size, "sha256": a.sha256} for a in m.attachments]
            out_list.append({
                "message_id": m.message_id,
                "subject": m.subject,
                "from": m.from_address,
                "to": m.to_address,
                "date": m.date,
                "attachments": att_info,
                "has_attachments": len(m.attachments) > 0,
            })
        return {"emails": out_list}


# --- Tool 2: read_email ---

class ReadEmailInput(BaseModel):
    message_id: str


class ReadEmailOutput(BaseModel):
    message_id: str
    subject: str
    from_address: str
    body_text: str
    attachments: List[Dict[str, Any]]
    injection_detected: bool = False


class ReadEmailTool(Tool):
    name = "read_email"
    description = "Reads email content and checks untrusted headers and body for prompt injection."
    risk = RiskLevel.READ
    args_schema = ReadEmailInput
    output_schema = ReadEmailOutput

    def __init__(self, mailbox: Optional[Mailbox] = None) -> None:
        self.mailbox = mailbox or FolderMailbox()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        msg_id = kwargs["message_id"]
        msg = self.mailbox.get_email(msg_id)
        if not msg:
            raise FileNotFoundError(f"Email with Message-ID '{msg_id}' not found.")

        # Check prompt injection in subject or body
        combined_text = f"{msg.subject}\n\n{msg.body_text}"
        is_inj, snip = PolicyEngine.detect_prompt_injection(combined_text)
        if is_inj:
            state.add_injection_flag(
                source=f"email:{msg_id}",
                snippet=snip,
                action_taken="Wrapped untrusted email content and marked flag",
            )

        att_meta = [{"filename": a.filename, "content_type": a.content_type, "size": a.size, "sha256": a.sha256} for a in msg.attachments]

        # Wrap untrusted body
        wrapped_body = f"<UNTRUSTED_EXTERNAL_EMAIL>\n{msg.body_text}\n</UNTRUSTED_EXTERNAL_EMAIL>"

        return {
            "message_id": msg.message_id,
            "subject": msg.subject,
            "from_address": msg.from_address,
            "body_text": wrapped_body,
            "attachments": att_meta,
            "injection_detected": is_inj,
        }


# --- Tool 3: save_attachment ---

class SaveAttachmentInput(BaseModel):
    message_id: str
    attachment_name: str
    dest_path: Optional[str] = None


class SaveAttachmentOutput(BaseModel):
    saved_path: str
    size_bytes: int
    sha256: str


class SaveAttachmentTool(Tool):
    name = "save_attachment"
    description = "Saves an email attachment to the run artifacts folder. Only .xlsx files are permitted."
    risk = RiskLevel.WRITE_LOW
    args_schema = SaveAttachmentInput
    output_schema = SaveAttachmentOutput

    def __init__(self, mailbox: Optional[Mailbox] = None) -> None:
        self.mailbox = mailbox or FolderMailbox()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        msg_id = kwargs["message_id"]
        att_name = kwargs["attachment_name"]

        # Prevent path traversal: sanitize base filename
        clean_name = os.path.basename(att_name).strip()
        clean_name = re.sub(r"[^\w\-\.]", "_", clean_name)

        if not clean_name.lower().endswith(".xlsx"):
            raise ValueError(f"Security policy violation: Only .xlsx questionnaire attachments may be saved (got '{clean_name}').")

        msg = self.mailbox.get_email(msg_id)
        if not msg:
            raise FileNotFoundError(f"Email with Message-ID '{msg_id}' not found.")

        target_att = None
        for a in msg.attachments:
            if a.filename.lower() == att_name.lower() or os.path.basename(a.filename).lower() == clean_name.lower():
                target_att = a
                break

        if not target_att:
            raise FileNotFoundError(f"Attachment '{att_name}' not found in email {msg_id}.")

        from agent.config import get_artifacts_dir
        artifacts_base = get_artifacts_dir()
        out_dir = os.path.join(artifacts_base, state.run_id, "inputs") if state.run_id else "data/inbox"
        os.makedirs(out_dir, exist_ok=True)
        dest = kwargs.get("dest_path") or os.path.join(out_dir, clean_name)

        # Path traversal guard on dest
        real_dest = os.path.abspath(dest)
        if ".." in dest or not (real_dest.startswith(os.path.abspath(artifacts_base)) or real_dest.startswith(os.path.abspath("data"))):
            raise ValueError("Path traversal attempt detected in dest_path.")

        with open(real_dest, "wb") as f:
            f.write(target_att.data)

        state.artifacts["saved_questionnaire"] = real_dest
        return {
            "saved_path": real_dest,
            "size_bytes": len(target_att.data),
            "sha256": target_att.sha256,
        }


# --- Tool 4: draft_reply ---

class DraftReplyInput(BaseModel):
    message_id: str
    attachment_path: str
    custom_note: Optional[str] = None


class DraftReplyOutput(BaseModel):
    to_address: str
    subject: str
    in_reply_to: str
    body_text: str
    attachment_path: str
    attachment_sha256: str
    draft_sha256: str


class DraftReplyTool(Tool):
    name = "draft_reply"
    description = "Drafts a reply email with deterministic Re: subject, validated recipient, and scrubbed body."
    risk = RiskLevel.WRITE_LOW
    args_schema = DraftReplyInput
    output_schema = DraftReplyOutput

    def __init__(self, mailbox: Optional[Mailbox] = None) -> None:
        self.mailbox = mailbox or FolderMailbox()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        msg_id = kwargs["message_id"]
        att_path = kwargs["attachment_path"]
        if not os.path.exists(att_path):
            raise FileNotFoundError(f"Attachment file not found: {att_path}")

        msg = self.mailbox.get_email(msg_id)
        if not msg:
            raise FileNotFoundError(f"Original email '{msg_id}' not found.")

        # Recipient determination: check Reply-To domain vs From domain
        sender_email = extract_email_address(msg.from_address)
        sender_domain = extract_domain(sender_email)

        to_addr = sender_email
        if msg.reply_to:
            reply_to_addr = extract_email_address(msg.reply_to)
            reply_to_domain = extract_domain(reply_to_addr)
            if reply_to_domain == sender_domain:
                to_addr = reply_to_addr
            else:
                # Cross-domain Reply-To rejected / ignored for security
                to_addr = sender_email

        # Deterministic Re: subject
        clean_subj = msg.subject
        if not clean_subj.lower().startswith("re:"):
            clean_subj = f"Re: {clean_subj}"

        with open(att_path, "rb") as f:
            att_bytes = f.read()
        att_hash = hashlib.sha256(att_bytes).hexdigest()

        # Build scrubbed body
        answered_cnt = sum(1 for a in state.answers if a.decision == AnswerDecision.ANSWERED)
        tot_cnt = len(state.questions) or len(state.answers)
        unresolved_cnt = tot_cnt - answered_cnt

        body = build_scrubbed_reply_body(
            customer_name=state.understood_goal.customer if state.understood_goal else "Customer",
            attachment_name=os.path.basename(att_path),
            answered_count=answered_cnt,
            total_count=tot_cnt,
            unresolved_count=unresolved_cnt,
        )

        draft_content = f"{to_addr}\n{clean_subj}\n{body}\n{att_hash}"
        draft_hash = hashlib.sha256(draft_content.encode("utf-8")).hexdigest()

        state.artifacts["email_draft_to"] = to_addr
        state.artifacts["email_draft_subject"] = clean_subj
        state.artifacts["email_draft_body"] = body
        state.artifacts["email_draft_attachment"] = att_path
        state.artifacts["email_draft_hash"] = draft_hash

        return {
            "to_address": to_addr,
            "subject": clean_subj,
            "in_reply_to": msg.message_id,
            "body_text": body,
            "attachment_path": att_path,
            "attachment_sha256": att_hash,
            "draft_sha256": draft_hash,
        }


# --- Tool 5: send_reply ---

class SendReplyInput(BaseModel):
    message_id: str
    attachment_path: str


class SendReplyOutput(BaseModel):
    sent_path: str
    recipient: str
    subject: str
    attachment_sha256: str
    message_id: str


class SendReplyTool(Tool):
    name = "send_reply"
    description = "Sends the reply email. Gated by WRITE_HIGH approval and strict domain authorization."
    risk = RiskLevel.WRITE_HIGH
    args_schema = SendReplyInput
    output_schema = SendReplyOutput

    def __init__(self, mailbox: Optional[Mailbox] = None, customers_config: Optional[str] = None) -> None:
        from agent.config import get_known_customers_file
        self.mailbox = mailbox or FolderMailbox()
        self.customers_config = customers_config or get_known_customers_file()

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        msg_id = kwargs["message_id"]
        att_path = kwargs["attachment_path"]

        # 1. Idempotency check: prevent duplicate send
        if state.artifacts.get("sent_email_path"):
            existing_sent = state.artifacts["sent_email_path"]
            if os.path.exists(existing_sent):
                return {
                    "sent_path": existing_sent,
                    "recipient": state.artifacts.get("email_draft_to", ""),
                    "subject": state.artifacts.get("email_draft_subject", ""),
                    "attachment_sha256": state.artifacts.get("sent_attachment_sha256", ""),
                    "message_id": f"sent-{state.run_id}",
                }

        # 2. Check FINAL_SIGNOFF approval
        appr = state.pending_approval
        if not appr or appr.status != ApprovalStatus.APPROVED or appr.kind != ApprovalKind.FINAL_SIGNOFF:
            raise PermissionError("Action blocked: send_reply requires approved FINAL_SIGNOFF.")

        # 3. Check attachment file
        if not os.path.exists(att_path):
            raise FileNotFoundError(f"Attachment file '{att_path}' does not exist.")

        with open(att_path, "rb") as f:
            att_data = f.read()
        att_hash = hashlib.sha256(att_data).hexdigest()

        # Check approval hash covers draft & attachment
        expected_draft_hash = state.artifacts.get("email_draft_hash")
        if appr.approved_answers_hash and expected_draft_hash:
            if appr.approved_answers_hash != expected_draft_hash:
                raise PermissionError("Approval hash mismatch: email draft or attachment modified after sign-off.")

        # 4. Check recipient against incoming sender & known_customers.json
        orig_msg = self.mailbox.get_email(msg_id)
        if not orig_msg:
            raise FileNotFoundError(f"Original email '{msg_id}' not found.")

        recipient = state.artifacts.get("email_draft_to") or extract_email_address(orig_msg.from_address)
        orig_sender = extract_email_address(orig_msg.from_address)

        # Enforce recipient must match original sender
        if recipient.lower() != orig_sender.lower():
            # Check if Reply-To was authorized (same domain)
            if orig_msg.reply_to and extract_email_address(orig_msg.reply_to).lower() == recipient.lower():
                if extract_domain(orig_msg.reply_to) != extract_domain(orig_sender):
                    raise PermissionError(f"Security policy violation: Cannot reply to cross-domain address {recipient}.")
            else:
                raise PermissionError(f"Security policy violation: Recipient {recipient} does not match original sender {orig_sender}.")

        # Check known customers domain
        if not is_known_customer_domain(recipient, self.customers_config):
            raise PermissionError(f"Security escalation: Recipient domain '{extract_domain(recipient)}' is not in known customers whitelist.")

        # 5. Build and scrub final EmailMessage
        subject = state.artifacts.get("email_draft_subject") or f"Re: {orig_msg.subject}"
        raw_body = state.artifacts.get("email_draft_body") or "Please find the completed questionnaire attached."
        scrubbed_body = scrub_customer_email_body(raw_body)

        out_msg = EmailMessage()
        out_msg["Subject"] = subject
        out_msg["From"] = "security-review@northwind.com"
        out_msg["To"] = recipient
        out_msg["In-Reply-To"] = f"<{orig_msg.message_id.strip('<>')}>"
        out_msg["References"] = f"<{orig_msg.message_id.strip('<>')}>"
        out_msg["Date"] = email.utils.format_datetime(_utcnow())
        out_msg["Message-ID"] = f"<secq-{state.run_id or 'delivery'}@northwind.com>"
        out_msg.set_content(scrubbed_body)

        out_msg.add_attachment(
            att_data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=os.path.basename(att_path),
        )

        # 6. Save to sent directory
        eml_bytes = out_msg.as_bytes()
        filename = f"{state.run_id}_reply.eml"
        sent_path = self.mailbox.save_sent_email(eml_bytes, filename)

        state.artifacts["sent_email_path"] = sent_path
        state.artifacts["sent_attachment_sha256"] = att_hash

        return {
            "sent_path": sent_path,
            "recipient": recipient,
            "subject": subject,
            "attachment_sha256": att_hash,
            "message_id": out_msg["Message-ID"],
        }

