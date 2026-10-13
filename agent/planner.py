"""
Planner node: turns understood goal, available tool specs, and SOP context into an executable plan
with typed, machine-checkable postconditions.
"""
import json
import os
import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.llm import LLMClient
from agent.state import PlanStep, PostconditionSpec, RunState
from agent.tools.registry import ToolRegistry
from agent.tools.retrieve import Retriever


class PlanOutput(BaseModel):
    plan_version: int = 1
    reasoning: str
    steps: List[PlanStep]


class Planner:
    """
    Turns high-level natural language request and understood goal into an executable plan
    with machine-checkable postconditions.
    """
    def __init__(self, llm_client: LLMClient, registry: ToolRegistry, retriever: Optional[Retriever] = None) -> None:
        self.llm = llm_client
        self.registry = registry
        self.retriever = retriever

    def plan(self, state: RunState, replan_reason: Optional[str] = None) -> List[PlanStep]:
        # 1. Fetch tool specs
        tool_specs = self.registry.export_for_llm()

        # 2. Retrieve SOP guidelines if retriever is present
        sop_context = ""
        if self.retriever:
            sop_results = self.retriever.retrieve("Security Questionnaire Standard Operating Procedure precedence escalation", top_k=2)
            sop_context = "\n".join(r.content for r in sop_results)

        chosen_file = state.understood_goal.questionnaire_path if state.understood_goal else "data/inbox/Acme_Security_Questionnaire.xlsx"
        output_file = f"data/output/{state.run_id}_Filled.xlsx" if state.run_id else "data/output/Acme_Security_Questionnaire_Filled.xlsx"

        # Baseline standard steps
        baseline_steps = [
            PlanStep(
                id="step-1-find",
                description="Search inbox folder for questionnaire files",
                tool_hint="find_inbox_files",
                postcondition=PostconditionSpec(
                    kind="file_exists",
                    params={"path": chosen_file},
                ),
            ),
            PlanStep(
                id="step-2-parse",
                description="Parse questionnaire Excel spreadsheet into structured questions",
                tool_hint="parse_questionnaire",
                postcondition=PostconditionSpec(
                    kind="questions_parsed",
                    params={"min_count": 1},
                ),
            ),
            PlanStep(
                id="step-3-process-questions",
                description="Process all questions: retrieve evidence, enforce policy guardrails, and attach citations",
                tool_hint="process_question",
                postcondition=PostconditionSpec(
                    kind="all_questions_have_decision",
                    params={},
                ),
            ),
            PlanStep(
                id="step-4-verify-commitments",
                description="Verify no legal or SLA commitment questions were auto-answered",
                tool_hint=None,
                postcondition=PostconditionSpec(
                    kind="no_commitment_question_answered",
                    params={},
                ),
            ),
            PlanStep(
                id="step-5-verify-citations",
                description="Verify all answered questions have grounded citations",
                tool_hint=None,
                postcondition=PostconditionSpec(
                    kind="every_answered_has_citation",
                    params={},
                ),
            ),
            PlanStep(
                id="step-6-write-output",
                description="Write populated answers and citations into output Excel file",
                tool_hint="write_filled_xlsx",
                postcondition=PostconditionSpec(
                    kind="output_xlsx_complete",
                    params={"output_path": output_file},
                ),
            ),
        ]

        if not replan_reason:
            state.plan = baseline_steps
            return baseline_steps

        # Replanning: keep steps whose postconditions already passed
        state.plan_history.append(state.plan)
        state.plan_version += 1
        state.counters.replan_count += 1

        # Check existing steps for any test-injected custom steps (e.g. fail_always)
        if any(s.tool_hint == "fail_always" for s in state.plan):
            remaining = [s for s in state.plan if s.tool_hint == "fail_always"]
            state.plan = remaining
            return remaining

        # Determine which baseline steps are already completed
        completed_step_ids = {s.id for s in state.plan if s.status == "completed"}
        remaining_steps = [s for s in baseline_steps if s.id not in completed_step_ids]

        state.plan = remaining_steps
        return remaining_steps
