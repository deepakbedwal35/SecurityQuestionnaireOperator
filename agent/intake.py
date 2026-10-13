"""
Unified intake node: handles incoming tasks originating from either an email or an uploaded xlsx.
Disambiguates email messages, extracts questionnaire attachments, checks domain authorization,
and establishes delivery requirements.
"""
import os
import re
from typing import Any, Dict, List, Optional
import openpyxl

from agent.llm import LLMClient
from agent.mail.mailbox import FolderMailbox, Mailbox, MailMessage
from agent.policy import PolicyEngine
from agent.state import RiskLevel, RunState, RunStatus, StepLog, UnderstoodGoal
from agent.tools.email import extract_domain, extract_email_address, is_known_customer_domain
from agent.tools.registry import ToolRegistry


class IntakeNode:
    """
    Unified intake node handling both email and uploaded questionnaire intake.
    """
    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        mailbox: Optional[Mailbox] = None,
        customers_config: Optional[str] = None,
    ) -> None:
        from agent.config import get_known_customers_file
        self.llm = llm_client
        self.registry = registry
        self.mailbox = mailbox or FolderMailbox()
        self.customers_config = customers_config or get_known_customers_file()

    def run(self, state: RunState) -> UnderstoodGoal:
        req = state.request_text.lower()

        # Check if the run was explicitly started with a file or upload hint
        is_upload = "upload" in req or req.startswith("uploaded:") or state.artifacts.get("uploaded_file") is not None
        # Check if an email id or email hint is provided
        is_email = "email" in req or "message-id" in req or "inbox" in req or req.startswith("handle email")

        if is_email:
            goal = self._handle_email_intake(state)
        else:
            goal = self._handle_upload_intake(state)

        state.understood_goal = goal
        return goal

    def _handle_upload_intake(self, state: RunState) -> UnderstoodGoal:
        uploaded_path = state.artifacts.get("uploaded_file")
        if not uploaded_path:
            # Fall back to finding candidate files in inbox folder
            inbox_tool = self.registry.get("find_inbox_files")
            inbox_res = inbox_tool.run(state, inbox_dir="data/inbox")
            candidate_files: List[str] = inbox_res.get("files", [])
            chosen = candidate_files[0] if candidate_files else "data/inbox/Acme_Security_Questionnaire.xlsx"
            rationale = "Selected questionnaire from inbox folder."
        else:
            chosen = uploaded_path
            rationale = f"Using uploaded file: {os.path.basename(uploaded_path)}"

        # Guess customer name from filename or request
        customer = "Acme"
        if "globex" in chosen.lower() or "globex" in state.request_text.lower():
            customer = "Globex"
        elif "initech" in chosen.lower() or "initech" in state.request_text.lower():
            customer = "Initech"

        goal = UnderstoodGoal(
            customer=customer,
            questionnaire_path=chosen,
            selection_rationale=rationale,
            source="upload",
            delivery="download",
            constraints=[],
        )

        state.observations.append(StepLog(
            tool="intake",
            input={"source": "upload", "request_text": state.request_text},
            output_summary=f"Understood upload intake for {customer}, path: {chosen}",
            risk=RiskLevel.READ,
        ))
        return goal

    def _handle_email_intake(self, state: RunState) -> UnderstoodGoal:
        emails = self.mailbox.list_emails()
        if not emails:
            # Fallback if no emails in mailbox
            return self._handle_upload_intake(state)

        # Match email by request text or select best candidate
        target_email: Optional[MailMessage] = None
        # Check if a specific message ID was mentioned
        for m in emails:
            if m.message_id.lower() in state.request_text.lower():
                target_email = m
                break

        if not target_email:
            # Filter emails with questionnaire attachments
            with_att = [m for m in emails if any(a.filename.lower().endswith(".xlsx") for a in m.attachments)]
            if with_att:
                # Match by customer keyword if possible
                req_lower = state.request_text.lower()
                matched = [m for m in with_att if any(cust in (m.subject.lower() + m.from_address.lower()) for cust in ["acme", "globex", "initech"] if cust in req_lower)]
                target_email = matched[0] if matched else with_att[0]
            else:
                target_email = emails[0]

        # Check prompt injection in email
        comb = f"{target_email.subject}\n\n{target_email.body_text}"
        is_inj, inj_snip = PolicyEngine.detect_prompt_injection(comb)
        if is_inj:
            state.add_injection_flag(
                source=f"email:{target_email.message_id}",
                snippet=inj_snip,
                action_taken="Wrapped untrusted email and escalated",
            )

        # Check known customer domain
        sender_email = extract_email_address(target_email.from_address)
        sender_domain = extract_domain(sender_email)
        is_known = is_known_customer_domain(sender_email, self.customers_config)

        # Look for attachments
        xlsx_atts = [a for a in target_email.attachments if a.filename.lower().endswith(".xlsx")]
        non_xlsx_atts = [a for a in target_email.attachments if not a.filename.lower().endswith(".xlsx")]

        customer_name = "Acme" if "acme" in sender_domain else (sender_domain.split(".")[0].capitalize() if sender_domain else "Customer")

        # Save attachment to artifacts if present
        chosen_path = None
        rationale = ""
        low_confidence = False
        needs_clarification = False

        if not is_known:
            rationale = f"Unknown sender domain '{sender_domain}': requires human confirmation."
            low_confidence = True
            needs_clarification = True
        elif not xlsx_atts:
            if non_xlsx_atts:
                rationale = f"Email contains non-xlsx attachments ({[a.filename for a in non_xlsx_atts]}): only .xlsx supported."
            else:
                rationale = "Email contains no questionnaire attachments."
            low_confidence = True
            needs_clarification = True
        elif len(xlsx_atts) > 1:
            # 2 attachments: check if one is old/stale or pick primary
            stale_match = [a for a in xlsx_atts if any(w in a.filename.lower() for w in ["old", "stale", "v1"])]
            active = [a for a in xlsx_atts if not any(w in a.filename.lower() for w in ["old", "stale", "v1"])]
            if len(active) == 1:
                target_att = active[0]
                rationale = f"Selected {target_att.filename} over stale attachment."
            else:
                target_att = xlsx_atts[0]
                rationale = f"Multiple attachments found ({[a.filename for a in xlsx_atts]}): selected {target_att.filename}."
                low_confidence = True
                needs_clarification = True

            from agent.config import get_artifacts_dir
            out_dir = os.path.join(get_artifacts_dir(), state.run_id, "inputs") if state.run_id else "data/inbox"
            os.makedirs(out_dir, exist_ok=True)
            chosen_path = os.path.join(out_dir, target_att.filename)
            with open(chosen_path, "wb") as f:
                f.write(target_att.data)
            state.artifacts["saved_questionnaire"] = chosen_path
        else:
            from agent.config import get_artifacts_dir
            target_att = xlsx_atts[0]
            out_dir = os.path.join(get_artifacts_dir(), state.run_id, "inputs") if state.run_id else "data/inbox"
            os.makedirs(out_dir, exist_ok=True)
            chosen_path = os.path.join(out_dir, target_att.filename)
            with open(chosen_path, "wb") as f:
                f.write(target_att.data)
            state.artifacts["saved_questionnaire"] = chosen_path
            rationale = f"Saved questionnaire attachment: {target_att.filename}"

        # If unknown sender or no attachments or clarification needed, escalate run
        if not is_known or needs_clarification:
            state.status = RunStatus.ESCALATED
            state.failure_reason = rationale

        goal = UnderstoodGoal(
            customer=customer_name,
            questionnaire_path=chosen_path or "data/inbox/Acme_Security_Questionnaire.xlsx",
            selection_rationale=rationale,
            source="email",
            source_message_id=target_email.message_id,
            sender=sender_email,
            reply_to_thread=target_email.in_reply_to or target_email.message_id,
            delivery="email_reply",
            low_confidence_goal=low_confidence,
            needs_human_clarification=needs_clarification,
            email_body=target_email.body_text,
        )

        state.observations.append(StepLog(
            tool="intake",
            input={"source": "email", "message_id": target_email.message_id, "from": sender_email},
            output_summary=f"Understood email intake from {sender_email}. Attached: {chosen_path}. Rationale: {rationale}",
            risk=RiskLevel.READ,
        ))

        return goal
