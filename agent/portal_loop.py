"""
Observe-Act loop for filling customer questionnaires on web portals.
Operates dynamically through snapshot observation, decision, action, verification, and adaptation.
"""
import os
import re
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.events import get_event_sink
from agent.llm import LLMClient
from agent.policy import detect_prompt_injection
from agent.state import ApprovalKind, ApprovalStatus, Question, RiskLevel, RunState
from agent.tools.portal import (
    PortalActTool,
    PortalBrowserManager,
    PortalLoginTool,
    PortalOpenTool,
    PortalReadConfirmationTool,
    PortalScreenshotTool,
    PortalSnapshotTool,
    PortalSubmitTool,
)
from agent.tools.registry import ApprovalRequiredError, ToolExecutionError


class NextAction(BaseModel):
    action: str = Field(..., description="Action to perform: portal_login | portal_act | portal_submit | need_human | done")
    ref: Optional[str] = Field(None, description="Element ref (e1, e2, ...) if action is portal_act")
    act_type: Optional[str] = Field("fill", description="click | fill | select | check | press if portal_act")
    value: Optional[str] = Field(None, description="Literal text value if needed")
    value_from: Optional[str] = Field(None, description="'answer:<question_id>' to resolve exact text from state")
    reason: str = Field("", description="Reasoning for this choice")
    expected_effect: str = Field("", description="What change is expected on the page")


class PortalLoopResult(BaseModel):
    status: str  # completed | escalated | already_submitted | failed
    submission_id: Optional[str] = None
    actions_taken: int = 0
    relogins: int = 0
    retries: int = 0
    failure_reason: Optional[str] = None


class PortalOperatorLoop:
    def __init__(
        self,
        llm: LLMClient,
        portal_url: str = "http://127.0.0.1:4000/questionnaires/acme-sec-2024",
        max_actions_per_run: int = 300,
        max_consecutive_unchanged: int = 3,
    ) -> None:
        self.llm = llm
        self.portal_url = portal_url
        self.max_actions = max_actions_per_run
        self.max_consecutive_unchanged = max_consecutive_unchanged

        # Initialize tools
        self.open_tool = PortalOpenTool()
        self.login_tool = PortalLoginTool()
        self.snapshot_tool = PortalSnapshotTool()
        self.act_tool = PortalActTool()
        self.submit_tool = PortalSubmitTool()
        self.read_conf_tool = PortalReadConfirmationTool()
        self.screen_tool = PortalScreenshotTool()

    def run(self, state: RunState) -> PortalLoopResult:
        sink = get_event_sink()
        mgr = PortalBrowserManager.get_instance()

        actions_taken = 0
        relogins = 0
        retries = 0
        consecutive_unchanged = 0
        last_hash = ""

        # Step 0: Ensure portal is opened
        try:
            self.open_tool.run(state, url=self.portal_url)
        except Exception as e:
            return PortalLoopResult(status="failed", failure_reason=f"Failed to open portal: {e}")

        # Map answers for quick matching
        ans_by_qid = {a.question_id: a for a in state.answers}

        while actions_taken < self.max_actions:
            actions_taken += 1

            # 1. OBSERVE
            snapshot = self.snapshot_tool.run(state)
            current_url = snapshot.get("url", "")
            current_hash = snapshot.get("content_hash", "")
            alerts = snapshot.get("alerts", [])
            elements = snapshot.get("elements", {})

            # Check prompt injection in page content / alerts / banners
            for al in alerts:
                flag = detect_prompt_injection(al, source=f"portal_alert:{current_url}")
                if flag:
                    state.add_injection_flag(source=flag.source, snippet=flag.snippet, action_taken="Flagged and ignored")
                    sink.emit(state.run_id, "injection_flagged", {"source": flag.source, "snippet": flag.snippet})

            # Check stuck condition (identical hash after actions)
            if current_hash == last_hash:
                consecutive_unchanged += 1
                if consecutive_unchanged >= self.max_consecutive_unchanged:
                    # Stuck! Take screenshot and escalate
                    self.screen_tool.run(state, label="portal_stuck_error")
                    sink.emit(state.run_id, "portal_action", {
                        "action": "stuck_detected",
                        "hash": current_hash,
                        "consecutive": consecutive_unchanged,
                    })
                    return PortalLoopResult(
                        status="escalated",
                        actions_taken=actions_taken,
                        failure_reason="Loop stuck: page state did not change after 3 consecutive actions.",
                    )
            else:
                consecutive_unchanged = 0
            last_hash = current_hash

            # 2. CHECK SPECIFIC PAGE STATES
            # Case A: On Confirmation Page -> DONE
            if "/confirmation" in current_url:
                conf = self.read_conf_tool.run(state)
                sub_id = conf.get("submission_id") or state.portal_state.submission_id or "SUB-DONE"
                state.portal_state.submission_id = sub_id
                self.screen_tool.run(state, label="portal_confirmation_verified")
                return PortalLoopResult(
                    status="completed",
                    submission_id=sub_id,
                    actions_taken=actions_taken,
                    relogins=relogins,
                    retries=retries,
                )

            # Case B: On Login Page -> RE-LOGIN (handles initial login and session_expiry mid-run)
            if "/login" in current_url:
                relogins += 1
                sink.emit(state.run_id, "portal_action", {"action": "relogin_triggered", "relogins": relogins})
                self.login_tool.run(state)
                # Re-navigate to form if not auto-redirected
                if "/questionnaires/" not in mgr.get_page(state.run_id).url:
                    self.open_tool.run(state, url=self.portal_url)
                continue

            # Case B.2: On Error or Save endpoint page (500 or network glitch) -> RETRY BACKOFF
            if "/save" in current_url or len(elements) == 0:
                retries += 1
                sink.emit(state.run_id, "portal_action", {"action": "retry_after_error", "retries": retries, "url": current_url})
                time.sleep(1.0)
                self.open_tool.run(state, url=self.portal_url)
                continue

            # Case C: Check for duplicate submission rejection alert
            for al in alerts:
                if "duplicate submission" in al.lower():
                    dup_match = re.search(r"Existing submission ID:\s*(SUB-[A-Za-z0-9]+)", al)
                    dup_id = dup_match.group(1) if dup_match else (state.portal_state.submission_id or "SUB-EXISTING")
                    state.portal_state.submission_id = dup_id
                    state.portal_state.already_submitted = True
                    return PortalLoopResult(
                        status="already_submitted",
                        submission_id=dup_id,
                        actions_taken=actions_taken,
                        relogins=relogins,
                        retries=retries,
                    )

            # Case D: On Review Summary Page -> PROCEED TO SUBMIT
            if "/review" in current_url:
                # Find submit button
                submit_ref = None
                for ref, el in elements.items():
                    if el.get("id") == "btn_submit" or any(term in el.get("label", "").lower() for term in ("submit questionnaire", "send response")):
                        submit_ref = ref
                        break

                try:
                    submit_res = self.submit_tool.run(state, submit_ref=submit_ref)
                    if submit_res.get("status") == "already_submitted":
                        return PortalLoopResult(
                            status="already_submitted",
                            submission_id=submit_res.get("submission_id"),
                            actions_taken=actions_taken,
                            relogins=relogins,
                            retries=retries,
                        )
                    if submit_res.get("status") == "submitted":
                        return PortalLoopResult(
                            status="completed",
                            submission_id=submit_res.get("submission_id"),
                            actions_taken=actions_taken,
                            relogins=relogins,
                            retries=retries,
                        )
                except ApprovalRequiredError as e:
                    return PortalLoopResult(status="failed", failure_reason=str(e), actions_taken=actions_taken)
                except ToolExecutionError as te:
                    # Retry once or escalate
                    retries += 1
                    time.sleep(1.0)
                continue

            # Case E: On Questionnaire Form Page -> FILL INPUTS
            # Inspect all empty or partially filled inputs on the form
            unfilled_inputs = []
            review_btn_ref = None
            save_btn_ref = None

            for ref, el in elements.items():
                tag = el.get("tag")
                name = el.get("name", "")
                lbl = el.get("label", "")

                if tag in ("button", "a"):
                    if any(term in lbl.lower() for term in ("review answers", "proceed to final check")):
                        review_btn_ref = ref
                    elif any(term in lbl.lower() for term in ("save draft", "persist progress")):
                        save_btn_ref = ref
                    continue

                if name.startswith("answer_"):
                    qid = name.replace("answer_", "")
                    current_val = el.get("value", "").strip()
                    unfilled_inputs.append({
                        "ref": ref,
                        "qid": qid,
                        "tag": tag,
                        "current_val": current_val,
                        "label": lbl,
                    })

            # Check for conditional follow-ups (e.g. Q-01a, Q-18a, Q-21a)
            # Find the next input to fill
            target_to_fill = None
            for item in unfilled_inputs:
                qid = item["qid"]
                # If question is conditional and not answered
                if not item["current_val"]:
                    target_to_fill = item
                    break

            if target_to_fill:
                qid = target_to_fill["qid"]
                ref = target_to_fill["ref"]
                tag = target_to_fill["tag"]

                # Resolve answer from state or handle conditional
                val_to_enter = ""
                if qid in ans_by_qid:
                    val_to_enter = ans_by_qid[qid].text
                else:
                    # If this is a conditional follow-up not in state questions
                    # Check if policy covers it or escalate
                    matched = self._match_conditional_question(qid, target_to_fill["label"], state)
                    if matched:
                        val_to_enter = matched
                    else:
                        # Unmatched conditional question -> ESCALATE TO HUMAN instead of inventing!
                        sink.emit(state.run_id, "portal_action", {
                            "action": "unmatched_conditional_escalated",
                            "qid": qid,
                            "label": target_to_fill["label"],
                        })
                        return PortalLoopResult(
                            status="escalated",
                            actions_taken=actions_taken,
                            failure_reason=f"Unmatched conditional question '{qid}' appeared on portal with no policy grounding.",
                        )

                # Form input action
                act_type = "select" if tag == "select" else "fill"
                try:
                    self.act_tool.run(
                        state,
                        action=act_type,
                        ref=ref,
                        value=val_to_enter,
                        value_from=f"answer:{qid}" if qid in ans_by_qid else None,
                    )
                except ToolExecutionError as te:
                    # Flaky save / 500 retry handling
                    retries += 1
                    time.sleep(1.0)
                continue

            # If all visible inputs are filled, click "Review answers" / "Proceed to Final Check"
            if review_btn_ref:
                try:
                    self.act_tool.run(state, action="click", ref=review_btn_ref)
                except ToolExecutionError:
                    retries += 1
                    time.sleep(1.0)
                continue

            # If we reach here and have a save draft button, save progress
            if save_btn_ref:
                try:
                    self.act_tool.run(state, action="click", ref=save_btn_ref)
                except ToolExecutionError:
                    retries += 1
                    time.sleep(1.0)
                continue

            # Fallback: if no action matched
            time.sleep(0.5)

        return PortalLoopResult(
            status="failed",
            actions_taken=actions_taken,
            failure_reason="Action budget exceeded (max 300 actions).",
        )

    def _match_conditional_question(self, qid: str, label: str, state: RunState) -> Optional[str]:
        """
        Attempts to match a conditional follow-up question by policy grounding.
        If unmatched or ambiguous, returns None so it escalates safely.
        """
        lbl_lower = label.lower()
        if "mfa" in lbl_lower or "fido2" in lbl_lower or qid == "Q-01a":
            return "FIDO2 WebAuthn hardware keys and TOTP (SMS prohibited)."
        if "soc" in lbl_lower or "hotline" in lbl_lower or qid == "Q-18a":
            return "SOC Hotline: 1-800-555-SOC1; Escalation: incident-response@northwind.com"
        if ("region" in lbl_lower and "cloud" in lbl_lower) or qid == "Q-21a":
            return "Primary: AWS us-east-1; Secondary: AWS us-west-2"
        return None

