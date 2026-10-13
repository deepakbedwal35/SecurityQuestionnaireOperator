"""
Playwright-based browser manager and portal interaction tools.
Thread-safe sync Playwright API for the agent execution loop.
"""
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright

from agent.events import get_event_sink
from agent.state import ApprovalKind, ApprovalStatus, RiskLevel, RunState
from agent.tools.registry import ApprovalRequiredError, Tool, ToolExecutionError


# -------------------------------------------------------------------------
# Browser Lifecycle & Manager
# -------------------------------------------------------------------------
class PortalBrowserManager:
    """
    Manages Playwright browser contexts keyed by run_id.
    Browser and Page objects live strictly in-process and are NEVER pickled or put in state.
    """
    _instance: Optional["PortalBrowserManager"] = None

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._contexts: Dict[str, BrowserContext] = {}
        self._pages: Dict[str, Page] = {}
        self._snapshots: Dict[str, Dict[str, Any]] = {}  # run_id -> snapshot dict

    @classmethod
    def get_instance(cls) -> "PortalBrowserManager":
        if cls._instance is None:
            cls._instance = PortalBrowserManager()
        return cls._instance

    def _ensure_playwright(self) -> None:
        if self._playwright is None:
            self._playwright = sync_playwright().start()
            headed = os.environ.get("PORTAL_HEADED", "0") in ("1", "true", "True")
            self._browser = self._playwright.chromium.launch(headless=not headed)

    def get_page(self, run_id: str, storage_state_path: Optional[str] = None) -> Page:
        self._ensure_playwright()
        if run_id not in self._pages or self._pages[run_id].is_closed():
            context_kwargs = {}
            if storage_state_path and os.path.exists(storage_state_path):
                context_kwargs["storage_state"] = storage_state_path

            context = self._browser.new_context(**context_kwargs)
            page = context.new_page()
            self._contexts[run_id] = context
            self._pages[run_id] = page
        return self._pages[run_id]

    def set_latest_snapshot(self, run_id: str, snapshot: Dict[str, Any]) -> None:
        self._snapshots[run_id] = snapshot

    def get_latest_snapshot(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._snapshots.get(run_id)

    def close_run(self, run_id: str, state: Optional[RunState] = None) -> None:
        """Saves storage_state if context exists and closes page/context."""
        try:
            if run_id in self._contexts and state:
                storage_path = os.path.join("artifacts", run_id, "portal_storage_state.json")
                os.makedirs(os.path.dirname(storage_path), exist_ok=True)
                try:
                    self._contexts[run_id].storage_state(path=storage_path)
                    state.portal_state.storage_state_path = storage_path
                except Exception:
                    pass
            if run_id in self._pages:
                try:
                    self._pages[run_id].close()
                except Exception:
                    pass
                del self._pages[run_id]
            if run_id in self._contexts:
                try:
                    self._contexts[run_id].close()
                except Exception:
                    pass
                del self._contexts[run_id]
            if run_id in self._snapshots:
                del self._snapshots[run_id]
        except Exception:
            pass

    def close_all(self) -> None:
        for rid in list(self._pages.keys()):
            self.close_run(rid)
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


# -------------------------------------------------------------------------
# Tool 1: portal_open (READ)
# -------------------------------------------------------------------------
class PortalOpenInput(BaseModel):
    url: str = Field(..., description="Target portal URL to navigate to")


class PortalOpenTool(Tool):
    name = "portal_open"
    description = "Opens the specified portal URL in the browser context."
    risk = RiskLevel.READ
    args_schema = PortalOpenInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)
        url = kwargs["url"]
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        state.portal_state.current_url = page.url

        sink = get_event_sink()
        sink.emit(state.run_id, "portal_action", {
            "action": "portal_open",
            "url": url,
            "current_url": page.url,
        })
        return {"status": "opened", "url": page.url}


# -------------------------------------------------------------------------
# Tool 2: portal_login (WRITE_LOW)
# -------------------------------------------------------------------------
class PortalLoginInput(BaseModel):
    login_url: Optional[str] = Field(None, description="Optional explicit login URL if different from current page")


class PortalLoginTool(Tool):
    name = "portal_login"
    description = "Logs in using environment credentials (PORTAL_USER and PORTAL_PASS). Secrets are kept internal."
    risk = RiskLevel.WRITE_LOW
    args_schema = PortalLoginInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        user = os.environ.get("PORTAL_USER", "admin@acme.com")
        password = os.environ.get("PORTAL_PASS", "AcmeSecret2024!")

        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        login_url = kwargs.get("login_url")
        if login_url and page.url != login_url:
            page.goto(login_url, wait_until="domcontentloaded")
        elif "/login" not in page.url:
            portal_env = os.environ.get("PORTAL_URL", "http://127.0.0.1:4000")
            base_url = "/".join(page.url.split("/")[:3]) if "://" in page.url else ("/".join(portal_env.split("/")[:3]) if "://" in portal_env else portal_env)
            page.goto(f"{base_url}/login", wait_until="domcontentloaded")

        # Fill credentials using semantic HTML labels / types without logging secrets
        try:
            user_input = page.locator('input[type="text"], input[name="username"]').first
            pass_input = page.locator('input[type="password"], input[name="password"]').first
            submit_btn = page.locator('button[type="submit"], input[type="submit"]').first

            user_input.fill(user)
            pass_input.fill(password)
            submit_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception as e:
            raise ToolExecutionError(f"Login failed to interact with form: {e}")

        # Check for error alert
        error_loc = page.locator('[role="alert"], .error')
        if error_loc.count() > 0 and "invalid" in (error_loc.first.text_content() or "").lower():
            raise ToolExecutionError("Login failed: invalid credentials according to portal.")

        state.portal_state.logged_in = True
        state.portal_state.current_url = page.url

        # Save session state to disk
        storage_path = os.path.join("artifacts", state.run_id, "portal_storage_state.json")
        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
        try:
            page.context.storage_state(path=storage_path)
            state.portal_state.storage_state_path = storage_path
        except Exception:
            pass

        sink = get_event_sink()
        sink.emit(state.run_id, "portal_action", {
            "action": "portal_login",
            "logged_in": True,
            "current_url": page.url,
            # Note: password is never emitted
        })
        return {"status": "logged_in", "url": page.url}


# -------------------------------------------------------------------------
# Tool 3: portal_snapshot (READ)
# -------------------------------------------------------------------------
class PortalSnapshotInput(BaseModel):
    pass


class PortalSnapshotTool(Tool):
    name = "portal_snapshot"
    description = "Produces a compact accessibility snapshot of the current page with stable element references (e1, e2, ...)."
    risk = RiskLevel.READ
    args_schema = PortalSnapshotInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        url = page.url
        title = page.title()

        # Extract banners / alerts
        alerts: List[str] = []
        try:
            alert_elements = page.locator('[role="alert"], [role="status"]').all()
            for al in alert_elements:
                txt = al.text_content() or ""
                if txt.strip():
                    alerts.append(txt.strip())
        except Exception:
            pass

        # Extract interactable elements with stable indexing (e1, e2, ...)
        raw_elements = page.evaluate("""
            () => {
                const results = [];
                const interactables = document.querySelectorAll('button, a, input, select, textarea, [role="button"]');
                interactables.forEach((el, index) => {
                    // Check if element is visible
                    const rect = el.getBoundingClientRect();
                    const isVisible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(el).display !== 'none' && window.getComputedStyle(el).visibility !== 'hidden';
                    if (!isVisible) return;

                    let label = '';
                    if (el.labels && el.labels.length > 0) {
                        label = el.labels[0].innerText;
                    } else if (el.getAttribute('aria-label')) {
                        label = el.getAttribute('aria-label');
                    } else if (el.innerText) {
                        label = el.innerText.trim();
                    } else if (el.getAttribute('placeholder')) {
                        label = el.getAttribute('placeholder');
                    } else if (el.getAttribute('name')) {
                        label = el.getAttribute('name');
                    }

                    results.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        name: el.getAttribute('name') || '',
                        role: el.getAttribute('role') || el.tagName.toLowerCase(),
                        label: label.replace(/\\s+/g, ' ').trim(),
                        value: el.value !== undefined ? el.value : '',
                        checked: el.checked || false,
                        disabled: el.disabled || false,
                    });
                });
                return results;
            }
        """)

        elements_map: Dict[str, Dict[str, Any]] = {}
        for idx, el in enumerate(raw_elements, start=1):
            ref = f"e{idx}"
            elements_map[ref] = {
                "ref": ref,
                "tag": el["tag"],
                "id": el["id"],
                "name": el["name"],
                "role": el["role"],
                "label": el["label"][:120],  # trim long labels
                "value": str(el["value"])[:100],
                "checked": el["checked"],
                "disabled": el["disabled"],
            }

        # Compute content hash
        hash_input = json.dumps({"url": url, "alerts": alerts, "elements": elements_map}, sort_keys=True)
        content_hash = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()[:12]
        state.portal_state.last_snapshot_hash = content_hash
        state.portal_state.current_url = url

        snapshot_dict = {
            "url": url,
            "title": title,
            "content_hash": content_hash,
            "alerts": alerts,
            "elements": elements_map,
        }
        mgr.set_latest_snapshot(state.run_id, snapshot_dict)

        return snapshot_dict


# -------------------------------------------------------------------------
# Tool 4: portal_act (WRITE_LOW)
# -------------------------------------------------------------------------
class PortalActInput(BaseModel):
    action: str = Field(..., description="Action to perform: click | fill | select | check | press")
    ref: str = Field(..., description="Element reference (e1, e2, ...) from the latest snapshot")
    value: Optional[str] = Field(None, description="Direct string value if not using value_from")
    value_from: Optional[str] = Field(None, description="Resolve answer from state: 'answer:<question_id>'")


class PortalActTool(Tool):
    name = "portal_act"
    description = "Interacts with an element on the portal page using its stable snapshot ref (click, fill, select)."
    risk = RiskLevel.WRITE_LOW
    args_schema = PortalActInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        mgr = PortalBrowserManager.get_instance()
        snapshot = mgr.get_latest_snapshot(state.run_id)
        if not snapshot:
            raise ToolExecutionError("No active snapshot. Call 'portal_snapshot' first.")

        ref = kwargs["ref"]
        action = kwargs["action"].lower()
        elements = snapshot.get("elements", {})
        if ref not in elements:
            raise ToolExecutionError(f"Stale or invalid element ref '{ref}'. Refresh snapshot with portal_snapshot.")

        el_meta = elements[ref]

        # Resolve value if value_from is specified
        value = kwargs.get("value")
        val_from = kwargs.get("value_from")
        if val_from:
            if val_from.startswith("answer:"):
                qid = val_from.split("answer:")[1].strip()
                ans_map = {a.question_id: a.text for a in state.answers}
                if qid in ans_map:
                    value = ans_map[qid]
                else:
                    # Check questions
                    q_map = {q.id: q.text for q in state.questions}
                    if qid not in q_map:
                        raise ToolExecutionError(f"Question '{qid}' not found in state answers.")
                    value = ""
            else:
                raise ToolExecutionError(f"Unsupported value_from pattern: '{val_from}'")

        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        # Locate element using tag, id, name, or accessible label
        loc = None
        if el_meta.get("id"):
            loc = page.locator(f"#{el_meta['id']}").first
        elif el_meta.get("name"):
            loc = page.locator(f"[name='{el_meta['name']}']").first
        elif el_meta.get("label"):
            loc = page.get_by_label(el_meta["label"], exact=False).first

        if loc is None or loc.count() == 0:
            # Fallback by tag and order index if needed
            loc = page.get_by_role(el_meta.get("role", "button"), name=el_meta.get("label", "")).first

        if loc.count() == 0:
            raise ToolExecutionError(f"Could not locate element for ref '{ref}' on the live page.")

        before_hash = state.portal_state.last_snapshot_hash
        try:
            if action == "click":
                loc.click()
            elif action == "fill":
                loc.fill(value or "")
            elif action == "select":
                # For select dropdowns
                try:
                    loc.select_option(value or "")
                except Exception:
                    loc.select_option(label=value or "")
            elif action == "check":
                loc.check()
            elif action == "press":
                loc.press(value or "Enter")
            else:
                raise ToolExecutionError(f"Unsupported action '{action}'. Use click, fill, select, check, press.")

            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception as e:
            raise ToolExecutionError(f"Action '{action}' on '{ref}' failed: {e}")

        # Update state URL
        state.portal_state.current_url = page.url

        # Auto-refresh snapshot after act
        snap_tool = PortalSnapshotTool()
        new_snapshot = snap_tool.run(state)
        after_hash = new_snapshot.get("content_hash")

        sink = get_event_sink()
        sink.emit(state.run_id, "portal_action", {
            "action": f"portal_act:{action}",
            "ref": ref,
            "target": el_meta.get("label") or el_meta.get("id"),
            "snapshot_before": before_hash,
            "snapshot_after": after_hash,
            "url": page.url,
        })

        return {
            "status": "acted",
            "action": action,
            "ref": ref,
            "snapshot_before": before_hash,
            "snapshot_after": after_hash,
            "new_url": page.url,
        }


# -------------------------------------------------------------------------
# Tool 5: portal_screenshot (READ)
# -------------------------------------------------------------------------
class PortalScreenshotInput(BaseModel):
    label: str = Field(..., description="Label or description of the screenshot (e.g. final_submission)")


class PortalScreenshotTool(Tool):
    name = "portal_screenshot"
    description = "Captures a full-page screenshot of the customer portal and stores it in artifacts."
    risk = RiskLevel.READ
    args_schema = PortalScreenshotInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        safe_label = re.sub(r"[^a-zA-Z0-9_\-]", "_", kwargs["label"])
        out_dir = os.path.join("artifacts", state.run_id, "screens")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{safe_label}.png")

        page.screenshot(path=out_path, full_page=True)

        # Store in state artifacts
        state.artifacts[f"screenshot_{safe_label}"] = out_path

        sink = get_event_sink()
        sink.emit(state.run_id, "portal_action", {
            "action": "portal_screenshot",
            "label": safe_label,
            "artifact_path": out_path,
        })

        return {"status": "captured", "artifact_path": out_path}


# -------------------------------------------------------------------------
# Tool 6: portal_submit (WRITE_HIGH)
# -------------------------------------------------------------------------
class PortalSubmitInput(BaseModel):
    submit_ref: Optional[str] = Field(None, description="Optional ref of submit button if known from review page snapshot")


class PortalSubmitTool(Tool):
    name = "portal_submit"
    description = "Performs final submission on the customer portal. HIGH RISK: Irreversible action requiring approved FINAL_SIGNOFF."
    risk = RiskLevel.WRITE_HIGH
    args_schema = PortalSubmitInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        # 1. Enforce FINAL_SIGNOFF approval
        appr = state.pending_approval
        if not appr or appr.kind != ApprovalKind.FINAL_SIGNOFF or not appr.approved:
            raise ApprovalRequiredError("portal_submit refused: requires approved FINAL_SIGNOFF.")

        # 2. Enforce answers hash equality
        current_answers_repr = json.dumps([{"q": a.question_id, "t": a.text} for a in state.answers], sort_keys=True)
        current_hash = hashlib.sha256(current_answers_repr.encode("utf-8")).hexdigest()
        if appr.approved_answers_hash and appr.approved_answers_hash != current_hash:
            raise ApprovalRequiredError(
                f"portal_submit refused: answers were altered after approval. Approved hash: {appr.approved_answers_hash}, current: {current_hash}"
            )

        # 3. Idempotency: if state already has a submission_id
        if state.portal_state.submission_id:
            return {
                "status": "already_submitted",
                "submission_id": state.portal_state.submission_id,
                "message": "Submission already recorded in state.",
            }

        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        # Locate submit button
        submit_btn = None
        submit_ref = kwargs.get("submit_ref")
        if submit_ref:
            snapshot = mgr.get_latest_snapshot(state.run_id)
            if snapshot and submit_ref in snapshot.get("elements", {}):
                meta = snapshot["elements"][submit_ref]
                if meta.get("id"):
                    submit_btn = page.locator(f"#{meta['id']}").first
                elif meta.get("label"):
                    submit_btn = page.get_by_role("button", name=meta["label"]).first

        if submit_btn is None or submit_btn.count() == 0:
            submit_btn = page.locator('button#btn_submit, button[type="submit"], input[type="submit"]').first

        if submit_btn.count() == 0:
            raise ToolExecutionError("Could not locate submit control on the current review page.")

        # Click submit
        try:
            submit_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception as e:
            raise ToolExecutionError(f"Failed to submit: {e}")

        # Check for duplicate submission alert on page
        page_text = page.locator("body").text_content() or ""
        duplicate_match = re.search(r"Existing submission ID:\s*(SUB-[A-Za-z0-9]+)", page_text)
        if duplicate_match or "duplicate submission" in page_text.lower():
            dup_id = duplicate_match.group(1) if duplicate_match else "SUB-EXISTING"
            state.portal_state.submission_id = dup_id
            state.portal_state.already_submitted = True

            sink = get_event_sink()
            sink.emit(state.run_id, "portal_action", {
                "action": "portal_submit:duplicate",
                "submission_id": dup_id,
                "already_submitted": True,
            })
            return {
                "status": "already_submitted",
                "submission_id": dup_id,
                "message": f"Duplicate submission rejected by portal. Existing ID: {dup_id}",
            }

        # Parse submission ID on confirmation page
        sub_id_loc = page.locator("#submission_id, [aria-label='Submission ID']")
        if sub_id_loc.count() > 0:
            sub_id = sub_id_loc.first.text_content().strip()
        else:
            match = re.search(r"(SUB-[0-9]{4,6})", page_text)
            sub_id = match.group(1) if match else "SUB-UNKNOWN"

        state.portal_state.submission_id = sub_id
        state.portal_state.current_url = page.url

        # Take confirmation screenshot automatically
        screen_tool = PortalScreenshotTool()
        screen_tool.run(state, label="portal_confirmation")

        sink = get_event_sink()
        sink.emit(state.run_id, "portal_action", {
            "action": "portal_submit:confirmed",
            "submission_id": sub_id,
            "url": page.url,
        })

        return {
            "status": "submitted",
            "submission_id": sub_id,
            "confirmation_url": page.url,
        }


# -------------------------------------------------------------------------
# Tool 7: portal_read_confirmation (READ)
# -------------------------------------------------------------------------
class PortalReadConfirmationInput(BaseModel):
    pass


class PortalReadConfirmationTool(Tool):
    name = "portal_read_confirmation"
    description = "Reads confirmation details and submission ID from the confirmation page."
    risk = RiskLevel.READ
    args_schema = PortalReadConfirmationInput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        mgr = PortalBrowserManager.get_instance()
        page = mgr.get_page(state.run_id, state.portal_state.storage_state_path)

        text = page.locator("body").text_content() or ""
        sub_id = None
        id_loc = page.locator("#submission_id, [aria-label='Submission ID']")
        if id_loc.count() > 0:
            sub_id = id_loc.first.text_content().strip()
        else:
            match = re.search(r"(SUB-[0-9A-Za-z]+)", text)
            if match:
                sub_id = match.group(1)

        return {
            "submission_id": sub_id,
            "confirmed": "submitted" in text.lower() or "confirmed" in text.lower(),
            "url": page.url,
        }
