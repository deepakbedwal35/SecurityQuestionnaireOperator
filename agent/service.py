"""
Service layer for Security Questionnaire Operator.
Encapsulates thread execution, LangGraph checkpoints, SQLite events, run state, and approval resolution.
Keep all business logic here so API/UI remain thin wrappers.
"""
import concurrent.futures
import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from agent.events import EventSink, RunEvent
from agent.graph import create_agent_graph
from agent.llm import FakeLLM, GroqLLM, LLMClient
from agent.memory import FeedbackStore
from agent.state import (
    AnswerDecision,
    ApprovalDecision,
    ApprovalItem,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    ItemAction,
    QuestionType,
    RiskLevel,
    RunState,
    RunStatus,
)
from agent.tools.composite import ProcessQuestionTool
from agent.tools.files import FindInboxFilesTool, ParseQuestionnaireTool, WriteFilledXlsxTool
from agent.tools.finalize import FinalizeOutputTool
from agent.tools.human import RequestHumanTool
from agent.tools.registry import ToolRegistry
from agent.tools.retrieve import KeywordPolicyRetriever, RetrieveEvidenceTool
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


class ValidationError(ValueError):
    """Raised when an approval decision or request is invalid."""
    pass


class OperatorService:
    def __init__(
        self,
        db_path: Optional[str] = None,
        feedback_path: Optional[str] = None,
        policies_dir: str = "data/policies",
        llm_client: Optional[LLMClient] = None,
    ) -> None:
        self.db_path = db_path or os.environ.get("CHECKPOINT_DB", "data/runs.sqlite")
        fb_path = feedback_path or os.environ.get("FEEDBACK_STORE", "data/feedback_rules.json")
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        self.event_sink = EventSink(db_path=self.db_path)
        self.feedback_store = FeedbackStore(file_path=fb_path)
        self.policies_dir = policies_dir
        self._llm_client = llm_client

        # Threading pool & locks per run
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        self._locks_lock = threading.Lock()
        self._run_locks: Dict[str, threading.Lock] = {}
        # Active futures
        self._futures: Dict[str, concurrent.futures.Future] = {}

    def _get_run_lock(self, run_id: str) -> threading.Lock:
        with self._locks_lock:
            if run_id not in self._run_locks:
                self._run_locks[run_id] = threading.Lock()
            return self._run_locks[run_id]

    def _get_llm(self) -> LLMClient:
        if self._llm_client:
            return self._llm_client
        provider = os.environ.get("LLM_PROVIDER", "fake").lower()
        if provider == "fake":
            return FakeLLM()
        return GroqLLM()

    def _build_registry_and_retriever(self, llm: LLMClient):
        retriever = KeywordPolicyRetriever(policies_dir=self.policies_dir, feedback_store=self.feedback_store)
        registry = ToolRegistry()
        registry.register(FindInboxFilesTool())
        registry.register(ParseQuestionnaireTool())
        registry.register(WriteFilledXlsxTool())
        registry.register(FinalizeOutputTool())
        registry.register(RetrieveEvidenceTool(retriever=retriever, feedback_store=self.feedback_store))
        registry.register(ProcessQuestionTool(retriever=retriever, llm_client=llm, feedback_store=self.feedback_store))
        registry.register(RequestHumanTool())
        registry.register(PortalOpenTool())
        registry.register(PortalLoginTool())
        registry.register(PortalSnapshotTool())
        registry.register(PortalActTool())
        registry.register(PortalScreenshotTool())
        registry.register(PortalSubmitTool())
        registry.register(PortalReadConfirmationTool())
        return registry, retriever

    def _get_compiled_graph(self, conn: sqlite3.Connection, llm: Optional[LLMClient] = None):
        saver = SqliteSaver(conn)
        saver.setup()
        active_llm = llm or self._get_llm()
        registry, retriever = self._build_registry_and_retriever(active_llm)
        return create_agent_graph(
            llm_client=active_llm,
            registry=registry,
            retriever=retriever,
            feedback_store=self.feedback_store,
            event_sink=self.event_sink,
            checkpointer=saver,
            enable_interrupts=True,
        )

    def start_run(self, request_text: str, run_id: Optional[str] = None) -> RunState:
        rid = run_id or f"run-{uuid.uuid4().hex[:10]}"
        state = RunState(
            run_id=rid,
            request_text=request_text,
            status=RunStatus.RUNNING,
        )

        # Kick off background execution
        future = self._executor.submit(self._run_worker, rid, state, None)
        self._futures[rid] = future
        return state

    def _run_worker(self, run_id: str, initial_state: Optional[RunState], command: Optional[Command]) -> None:
        lock = self._get_run_lock(run_id)
        if not lock.acquire(blocking=False):
            # Already running on this run_id
            return

        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            graph = self._get_compiled_graph(conn)
            config = {"configurable": {"thread_id": run_id}}

            if command is not None:
                graph.invoke(command, config)
            elif initial_state is not None:
                graph.invoke(initial_state, config)
            conn.close()
        except Exception as e:
            self.event_sink.emit(run_id, "run_failed", {"error": str(e)})
        finally:
            lock.release()

    def get_run(self, run_id: str) -> Optional[RunState]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            graph = self._get_compiled_graph(conn)
            config = {"configurable": {"thread_id": run_id}}
            snapshot = graph.get_state(config)
            if not snapshot or not snapshot.values:
                return None
            state = RunState(**snapshot.values)

            # Check if execution is paused at an interrupt task
            for t in snapshot.tasks:
                if t.interrupts:
                    for intr in t.interrupts:
                        val = intr.value
                        if isinstance(val, dict) and "approval_id" in val:
                            state.status = RunStatus.WAITING_APPROVAL
                            items = [ApprovalItem(**it) for it in val.get("items", [])]
                            state.pending_approval = ApprovalRequest(
                                id=val["approval_id"],
                                run_id=run_id,
                                kind=ApprovalKind(val.get("kind", "ANSWER_REVIEW")),
                                reason=val.get("reason", "Human approval required"),
                                items=items,
                                risk=RiskLevel.WRITE_HIGH,
                                status=ApprovalStatus.PENDING,
                            )
            return state
        except Exception as e:
            import traceback
            traceback.print_exc()
            return None
        finally:
            conn.close()

    def list_runs(self) -> List[Dict[str, Any]]:
        """
        Lists all runs recorded in events table.
        """
        runs = []
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT run_id FROM events ORDER BY run_id DESC")
            rows = cursor.fetchall()
            for row in rows:
                rid = row[0]
                run_state = self.get_run(rid)
                if run_state:
                    runs.append({
                        "run_id": run_state.run_id,
                        "status": run_state.status.value,
                        "created_at": run_state.created_at.isoformat(),
                        "request_text": run_state.request_text,
                    })
                else:
                    runs.append({
                        "run_id": rid,
                        "status": "unknown",
                        "created_at": "",
                        "request_text": "",
                    })
            return runs
        finally:
            conn.close()

    def get_events(self, run_id: str, after_seq: int = 0) -> List[RunEvent]:
        return self.event_sink.get_events(run_id, after_seq=after_seq)

    def get_pending_approval(self, run_id: str) -> Optional[ApprovalRequest]:
        state = self.get_run(run_id)
        if not state:
            return None
        if state.pending_approval and state.pending_approval.status == ApprovalStatus.PENDING:
            return state.pending_approval
        return None

    def resolve_approval(self, run_id: str, approval_id: str, decision: ApprovalDecision) -> RunState:
        """
        Validates decision, enforces legal commitment & citation constraints,
        and resumes LangGraph thread via Command(resume=decision).
        """
        state = self.get_run(run_id)
        if not state:
            raise ValueError(f"Run '{run_id}' not found.")

        pending = state.pending_approval
        # Idempotency check: if approval_id was already resolved, return current state without re-running
        if self.event_sink.has_event(run_id, "approval_resolved", approval_id):
            return state

        # If no pending approval or already resolved
        if not pending or pending.status != ApprovalStatus.PENDING:
            return state

        if pending.id != approval_id and decision.approval_id != pending.id:
            raise ValueError(f"Approval ID mismatch: expected '{pending.id}', got '{approval_id}'")

        # Validation rules
        if pending.kind == ApprovalKind.ANSWER_REVIEW:
            if not decision.decisions:
                raise ValidationError("ANSWER_REVIEW approval requires decisions for items.")

            # Map questions
            q_map = {q.id: q for q in state.questions}
            ans_map = {a.question_id: a for a in state.answers}

            for item_dec in decision.decisions:
                qid = item_dec.question_id
                q_obj = q_map.get(qid)
                ans_obj = ans_map.get(qid)

                if item_dec.action == ItemAction.APPROVE:
                    # Constraint: commitment questions can never be auto-approved
                    if q_obj and q_obj.type == QuestionType.COMMITMENT:
                        raise ValidationError(
                            f"Cannot approve commitment question '{qid}'. Must edit with human answer or reject."
                        )
                    # Constraint: no draft or no citations -> cannot approve
                    if not ans_obj or not ans_obj.text or ans_obj.decision != AnswerDecision.ANSWERED:
                        raise ValidationError(
                            f"Cannot approve question '{qid}' with no existing draft answer."
                        )
                    if not ans_obj.citations:
                        raise ValidationError(
                            f"Cannot approve question '{qid}' without grounded citations."
                        )

                elif item_dec.action == ItemAction.EDIT:
                    if not item_dec.edited_answer or not item_dec.edited_answer.strip():
                        raise ValidationError(f"Action 'edit' on question '{qid}' requires non-empty edited_answer.")

        elif pending.kind == ApprovalKind.FINAL_SIGNOFF:
            if not decision.final_action:
                decision.final_action = ItemAction.APPROVE

        # Submit resume command in background thread
        cmd = Command(resume=decision)
        future = self._executor.submit(self._run_worker, run_id, None, cmd)
        self._futures[run_id] = future

        # Wait briefly for synchronous resumption to process
        try:
            future.result(timeout=5.0)
        except Exception:
            pass

        updated_state = self.get_run(run_id)
        return updated_state or state

    def cancel_run(self, run_id: str) -> bool:
        lock = self._get_run_lock(run_id)
        with lock:
            state = self.get_run(run_id)
            if not state or state.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.REJECTED_BY_HUMAN):
                return False
            state.status = RunStatus.FAILED
            state.failure_reason = "Cancelled by user"
            # Emit cancelled event
            self.event_sink.emit(run_id, "run_failed", {"failure_reason": "Cancelled by user"})
            # Clean up Playwright browser context
            try:
                PortalBrowserManager.get_instance().close_run(run_id, state)
            except Exception:
                pass
            return True

    def get_report(self, run_id: str) -> Dict[str, Any]:
        state = self.get_run(run_id)
        if not state:
            return {"error": f"Run '{run_id}' not found."}

        report_md_path = state.artifacts.get("report_md") or f"artifacts/{run_id}/report.md"
        evidence_json_path = state.artifacts.get("evidence_json") or f"artifacts/{run_id}/evidence.json"

        report_md = ""
        evidence_data = {}
        if os.path.exists(report_md_path):
            with open(report_md_path, "r", encoding="utf-8") as f:
                report_md = f.read()

        if os.path.exists(evidence_json_path):
            with open(evidence_json_path, "r", encoding="utf-8") as f:
                try:
                    evidence_data = json.load(f)
                except Exception:
                    pass

        return {
            "run_id": run_id,
            "status": state.status.value,
            "report_md": report_md,
            "evidence": evidence_data,
            "artifacts": state.artifacts,
        }
