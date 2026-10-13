"""
Executor loop: picks next tool via LLM tool-calling, enforces step, retry, and replan limits,
and verifies step postconditions using the independent Verifier.
"""
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from agent.llm import LLMClient, PermanentError, ToolCall, TransientError, classify_exception
from agent.planner import Planner
from agent.state import PlanStep, RiskLevel, RunState, RunStatus, StepLog
from agent.tools.registry import ToolRegistry
from agent.verifier import Verifier


class ExecutorConfig(BaseModel):
    max_steps_per_run: int = 120
    max_retries_per_tool: int = 2
    max_replans: int = 3
    retry_backoff_base_s: float = 0.05  # Short for fast tests


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        llm_client: LLMClient,
        planner: Planner,
        config: Optional[ExecutorConfig] = None,
    ) -> None:
        self.registry = registry
        self.llm = llm_client
        self.planner = planner
        self.config = config or ExecutorConfig()

    def run_loop(self, state: RunState) -> RunState:
        """
        Executes the plan steps in sequence.
        For each step:
        1. Calls appropriate tool(s) until step action complete.
        2. Evaluates postcondition via Verifier.
        3. If postcondition fails, triggers retry / replan.
        """
        current_step_idx = 0

        while current_step_idx < len(state.plan):
            # Guard against infinite loops / max steps
            if state.counters.step_count >= self.config.max_steps_per_run:
                state.status = RunStatus.FAILED
                state.failure_reason = f"Exceeded max_steps_per_run ({self.config.max_steps_per_run})"
                return state

            step = state.plan[current_step_idx]
            step.status = "in_progress"

            # Execute tool action for this step if tool_hint is present
            try:
                step_success = self._execute_step_action(step, state)
            except PermanentError as pe:
                state.status = RunStatus.FAILED
                state.failure_reason = f"configuration/permanent error: {pe}"
                step.status = "failed"
                return state

            if not step_success:
                # Tool failed repeatedly
                if state.counters.replan_count < self.config.max_replans:
                    self.planner.plan(state, replan_reason=f"Tool failed for step: {step.description}")
                    current_step_idx = 0
                    continue
                else:
                    state.status = RunStatus.FAILED
                    state.failure_reason = f"Max replans ({self.config.max_replans}) reached after tool failure in {step.id}"
                    return state

            # Check postcondition with independent Verifier
            post_result = Verifier.verify(step.postcondition, state, step_id=step.id)
            if post_result.passed:
                step.status = "completed"
                current_step_idx += 1
            else:
                # Postcondition failed: trigger replan or fail
                step.status = "failed"
                if state.counters.replan_count < self.config.max_replans:
                    self.planner.plan(
                        state,
                        replan_reason=f"Postcondition '{step.postcondition.kind}' failed: {post_result.evidence}",
                    )
                    # Reset to first incomplete step of remaining plan
                    current_step_idx = 0
                    continue
                else:
                    state.status = RunStatus.FAILED
                    state.failure_reason = (
                        f"Max replans ({self.config.max_replans}) exceeded. "
                        f"Postcondition failed: {post_result.evidence}"
                    )
                    return state

        return state

    def _execute_step_action(self, step: PlanStep, state: RunState) -> bool:
        """
        Executes tool operations needed for the given plan step, supporting retry with backoff.
        Raises PermanentError immediately without retry.
        """
        tool_hint = step.tool_hint
        if not tool_hint:
            # Verification-only step (e.g. step 4, step 5)
            state.counters.step_count += 1
            return True

        # Special handling for composite process_question: run for all questions
        if tool_hint == "process_question":
            for q in state.questions:
                if state.counters.step_count >= self.config.max_steps_per_run:
                    return False
                success = self._invoke_tool_with_retry("process_question", {"question_id": q.id}, state)
                if not success:
                    return False
            return True

        # Standard tool execution
        tool_args: Dict[str, Any] = {}
        if tool_hint == "find_inbox_files":
            tool_args = {"inbox_dir": "data/inbox"}
        elif tool_hint == "parse_questionnaire":
            target = state.understood_goal.questionnaire_path if state.understood_goal else "data/inbox/Acme_Security_Questionnaire.xlsx"
            tool_args = {"file_path": target}
        elif tool_hint == "write_filled_xlsx":
            src = state.understood_goal.questionnaire_path if state.understood_goal else "data/inbox/Acme_Security_Questionnaire.xlsx"
            out = f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx"
            tool_args = {"source_file": src, "output_file": out}
        elif tool_hint == "finalize_output":
            src = f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx"
            dest = f"artifacts/{state.run_id}/final.xlsx"
            tool_args = {"source_file": src, "destination_file": dest}
        elif tool_hint == "draft_reply":
            msg_id = state.understood_goal.source_message_id if state.understood_goal else ""
            att_path = state.artifacts.get("filled_xlsx") or (f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx")
            tool_args = {"message_id": msg_id, "attachment_path": att_path}
        elif tool_hint == "send_reply":
            # If not yet approved or before interrupt, skip in executor (will be called in final signoff or after approval)
            if not state.pending_approval or state.pending_approval.status != ApprovalStatus.APPROVED:
                return True
            msg_id = state.understood_goal.source_message_id if state.understood_goal else ""
            att_path = f"artifacts/{state.run_id}/final.xlsx"
            tool_args = {"message_id": msg_id, "attachment_path": att_path}

        return self._invoke_tool_with_retry(tool_hint, tool_args, state)

    def _invoke_tool_with_retry(self, tool_name: str, args: Dict[str, Any], state: RunState) -> bool:
        retries = 0
        while retries <= self.config.max_retries_per_tool:
            state.counters.step_count += 1
            t0 = time.time()
            try:
                res = self.registry.execute(tool_name, state, **args)
                duration_ms = int((time.time() - t0) * 1000)
                state.observations.append(StepLog(
                    tool=tool_name,
                    input=args,
                    output_summary=f"Success: {str(res)[:100]}",
                    risk=self.registry.get(tool_name).risk,
                    duration_ms=duration_ms,
                ))
                return True
            except Exception as e:
                classified = classify_exception(e)
                duration_ms = int((time.time() - t0) * 1000)
                state.counters.tool_error_count += 1
                state.observations.append(StepLog(
                    tool=tool_name,
                    input=args,
                    output_summary=f"Failed (attempt {retries + 1}): {str(classified)}",
                    risk=self.registry.get(tool_name).risk,
                    duration_ms=duration_ms,
                    error=str(classified),
                ))

                # Permanent error -> abort immediately, no retries, no replan
                if isinstance(classified, PermanentError):
                    raise classified

                retries += 1
                if retries <= self.config.max_retries_per_tool:
                    backoff = self.config.retry_backoff_base_s * (2 ** (retries - 1))
                    time.sleep(backoff)
        return False
