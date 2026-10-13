"""
Understand node logic: extracts customer, candidate files, and constraints.
Handles candidate disambiguation and records reasoning in step logs.
"""
import os
import re
from typing import Any, Dict, List, Optional
import openpyxl
from agent.llm import LLMClient
from agent.state import RiskLevel, RunState, RunStatus, StepLog, UnderstoodGoal
from agent.tools.registry import ToolRegistry


class UnderstandNode:
    def __init__(self, llm_client: LLMClient, registry: ToolRegistry) -> None:
        self.llm = llm_client
        self.registry = registry

    def run(self, state: RunState) -> UnderstoodGoal:
        # 1. Search for candidate files in inbox
        inbox_tool = self.registry.get("find_inbox_files")
        inbox_res = inbox_tool.run(state, inbox_dir="data/inbox")
        candidate_files: List[str] = inbox_res.get("files", [])

        # Gather metadata for candidates
        candidates_meta: List[Dict[str, Any]] = []
        for path in candidate_files:
            mtime = os.path.getmtime(path) if os.path.exists(path) else 0
            size = os.path.getsize(path) if os.path.exists(path) else 0
            # Inspect sheet rows briefly
            row_count = 0
            try:
                wb = openpyxl.load_workbook(path, read_only=True)
                ws = wb.active
                row_count = ws.max_row or 0
            except Exception:
                pass
            candidates_meta.append({
                "path": path,
                "filename": os.path.basename(path),
                "mtime": mtime,
                "size_bytes": size,
                "approx_rows": row_count,
            })

        # Load understand prompt
        prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "understand.md")
        system_prompt = ""
        if os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as f:
                system_prompt = f.read()

        prompt_body = f"""
Request Text: "{state.request_text}"
Candidate Files:
{candidates_meta}
"""
        goal: UnderstoodGoal = self.llm.structured(
            prompt=prompt_body,
            schema=UnderstoodGoal,
            system=system_prompt,
        )

        # Disambiguation heuristic fallback if LLM returned generic path or multiple Acme files exist
        if not goal.questionnaire_path or not os.path.exists(goal.questionnaire_path):
            # Find matching candidate files
            matching = [c for c in candidates_meta if "acme" in c["filename"].lower()]
            if not matching:
                matching = candidates_meta

            if len(matching) == 1:
                goal.questionnaire_path = matching[0]["path"]
                goal.selection_rationale = f"Single matching candidate {matching[0]['filename']}."
            elif len(matching) > 1:
                # Disqualify files with old/stale/archive in name
                active = [c for c in matching if not any(w in c["filename"].lower() for w in ("old", "stale", "archive", "v1"))]
                if len(active) == 1:
                    goal.questionnaire_path = active[0]["path"]
                    goal.selection_rationale = f"Selected {active[0]['filename']} over stale/archived alternatives."
                elif len(active) > 1:
                    # Pick newest by mtime or rows
                    sorted_by_mtime = sorted(active, key=lambda x: (x["approx_rows"], x["mtime"]), reverse=True)
                    goal.questionnaire_path = sorted_by_mtime[0]["path"]
                    goal.selection_rationale = f"Selected {sorted_by_mtime[0]['filename']} (most complete, {sorted_by_mtime[0]['approx_rows']} rows)."
                else:
                    # All are marked old or ambiguous
                    goal.low_confidence_goal = True
                    goal.needs_human_clarification = True
                    goal.selection_rationale = "Multiple ambiguous candidate files; clarification needed."

        # Log understand step
        state.observations.append(StepLog(
            tool="understand",
            input={"request_text": state.request_text, "candidate_count": len(candidate_files)},
            output_summary=f"Understood customer: {goal.customer}, Path: {goal.questionnaire_path}, Rationale: {goal.selection_rationale}",
            risk=RiskLevel.READ,
        ))

        state.understood_goal = goal

        # If escalation is needed due to ambiguity
        if goal.needs_human_clarification:
            human_tool = self.registry.get("request_human_input")
            human_tool.run(
                state,
                reason=f"Ambiguous questionnaire selection: {goal.selection_rationale}",
                items=[{"candidates": [c["filename"] for c in candidates_meta]}],
            )
            state.status = RunStatus.ESCALATED

        return goal

