"""
Evidence and markdown report generator.
Produces:
- artifacts/<run_id>/evidence.json
- artifacts/<run_id>/report.md
"""
import hashlib
import json
import os
from typing import Any, Dict, List
from agent.state import AnswerDecision, InjectionFlag, PostconditionResult, RunState


def dedupe_injection_flags(raw_flags: List[Any]) -> List[Dict[str, str]]:
    """Deduplicates injection flags by (source, sha1(snippet))."""
    seen = set()
    deduped = []
    for fl in raw_flags:
        src = fl.get("source") if isinstance(fl, dict) else fl.source
        snip = fl.get("snippet") if isinstance(fl, dict) else fl.snippet
        act = fl.get("action_taken") if isinstance(fl, dict) else fl.action_taken
        key = (src, hashlib.sha1(snip.encode("utf-8")).hexdigest())
        if key not in seen:
            seen.add(key)
            deduped.append({
                "source": src,
                "snippet": snip,
                "action_taken": act,
            })
    return deduped


def dedupe_postcondition_results(results: List[PostconditionResult]) -> List[PostconditionResult]:
    """Deduplicates postcondition results, keeping the latest per kind per plan step."""
    latest_map = {}
    for r in results:
        key = (r.step_id or "default", r.kind)
        latest_map[key] = r
    return list(latest_map.values())


def generate_reports(state: RunState, artifacts_dir: str = "artifacts") -> Dict[str, str]:
    run_dir = os.path.join(artifacts_dir, state.run_id)
    os.makedirs(run_dir, exist_ok=True)

    evidence_json_path = os.path.join(run_dir, "evidence.json")
    report_md_path = os.path.join(run_dir, "report.md")

    # Counts
    parsed_count = len(state.questions)
    processed_count = len(state.answers)
    answered_count = sum(1 for a in state.answers if a.decision == AnswerDecision.ANSWERED)
    escalated_count = sum(1 for a in state.answers if a.decision == AnswerDecision.ESCALATED)
    flagged_count = sum(1 for a in state.answers if a.decision == AnswerDecision.FLAGGED)

    deduped_flags = dedupe_injection_flags(state.injection_flags)
    injection_count = len(deduped_flags)

    deduped_postconditions = dedupe_postcondition_results(state.postcondition_results)

    # 1. evidence.json
    evidence_data = {
        "run_id": state.run_id,
        "created_at": state.created_at.isoformat(),
        "status": state.status.value,
        "failure_reason": state.failure_reason,
        "request_text": state.request_text,
        "understood_goal": state.understood_goal.model_dump() if state.understood_goal else None,
        "plan_version": state.plan_version,
        "counters": state.counters.model_dump(),
        "artifacts": state.artifacts,
        "counts": {
            "parsed": parsed_count,
            "processed": processed_count,
            "answered": answered_count,
            "escalated": escalated_count,
            "flagged": flagged_count,
            "injections": injection_count,
        },
        "questions_count": parsed_count,
        "answers_count": processed_count,
        "answers": [a.model_dump() for a in state.answers],
        "injection_flags": deduped_flags,
        "postcondition_results": [r.model_dump() for r in deduped_postconditions],
        "step_log": [log.model_dump() for log in state.observations],
    }

    with open(evidence_json_path, "w", encoding="utf-8") as f:
        json.dump(evidence_data, f, indent=2, default=str)

    # 2. report.md
    md_lines = [
        f"# Security Questionnaire Operator - Execution Report",
        f"**Run ID:** `{state.run_id}`  ",
        f"**Status:** `{state.status.value.upper()}`  ",
    ]

    if state.failure_reason:
        md_lines.extend([
            f"",
            f"> [!CAUTION]",
            f"> **FAILURE REASON:** {state.failure_reason}",
            f"",
        ])

    md_lines.extend([
        f"**Customer:** {state.understood_goal.customer if state.understood_goal else 'N/A'}  ",
        f"**Input Questionnaire:** `{state.understood_goal.questionnaire_path if state.understood_goal else 'N/A'}`  ",
        f"**Filled Output:** `{state.artifacts.get('filled_xlsx', 'N/A')}`  ",
        "",
        "## Executive Summary",
        f"- **Parsed: {parsed_count}, Processed: {processed_count}, Answered: {answered_count}, Escalated: {escalated_count}, Flagged: {flagged_count}**",
        f"- **Prompt Injections Neutralized:** {injection_count}",
        f"- **Plan Versions:** {state.plan_version} (Replans: {state.counters.replan_count})",
        f"- **Total Execution Steps:** {state.counters.step_count} (Tool Errors: {state.counters.tool_error_count})",
        "",
        "## Machine Verification Results",
        "| Step | Postcondition Kind | Status | Evidence |",
        "| --- | --- | --- | --- |",
    ])

    for p in deduped_postconditions:
        status_icon = "PASSED" if p.passed else "FAILED"
        step_label = p.step_id or "default"
        md_lines.append(f"| `{step_label}` | `{p.kind}` | **{status_icon}** | {p.evidence} |")

    md_lines.extend([
        "",
        "## Security Injection Flags",
    ])
    if deduped_flags:
        for fl in deduped_flags:
            md_lines.append(f"- **Source:** `{fl['source']}` | **Action:** {fl['action_taken']} | **Pattern:** `{fl['snippet']}`")
    else:
        md_lines.append("_No prompt injection flags encountered._")

    md_lines.extend([
        "",
        "## Portal Submission & Web Evidence",
    ])
    if state.portal_state.submission_id:
        md_lines.append(f"- **Submission ID:** `{state.portal_state.submission_id}`")
        md_lines.append(f"- **Portal URL:** `{state.portal_state.current_url or 'http://127.0.0.1:4000'}`")
        md_lines.append(f"- **Already Submitted Flag:** `{state.portal_state.already_submitted}`")
    else:
        md_lines.append("_No portal submission recorded for this run._")

    screenshots = [p for k, p in state.artifacts.items() if k.startswith("screenshot_")]
    if screenshots:
        md_lines.append(f"- **Captured Screenshots:** {len(screenshots)} proof images")
        for sc in screenshots:
            md_lines.append(f"  - `{sc}`")

    md_lines.extend([
        "",
        "## Escalated Questions (Human Review Required)",
    ])
    escalated_answers = [a for a in state.answers if a.decision == AnswerDecision.ESCALATED]
    if escalated_answers:
        for ea in escalated_answers:
            q_obj = next((q for q in state.questions if q.id == ea.question_id), None)
            q_text = q_obj.text if q_obj else ea.question_id
            md_lines.append(f"- **[{ea.question_id}]** {q_text}")
            md_lines.append(f"  - _Reason:_ {ea.text}")
            if ea.citations:
                md_lines.append(f"  - _Citation:_ {ea.citations[0].doc}")
    else:
        md_lines.append("_No questions escalated._")

    md_lines.extend([
        "",
        "## Question-by-Question Decision Table",
        "| ID | Decision | Answer Summary | Citations | Confidence |",
        "| --- | --- | --- | --- | --- |",
    ])

    for a in state.answers:
        cit_str = ", ".join(c.doc for c in a.citations) if a.citations else "None"
        ans_clean = a.text.replace("\n", " ")[:70]
        md_lines.append(
            f"| `{a.question_id}` | `{a.decision.value}` | {ans_clean}... | {cit_str} | {a.confidence} |"
        )

    with open(report_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines) + "\n")

    state.artifacts["evidence_json"] = evidence_json_path
    state.artifacts["report_md"] = report_md_path

    return {
        "evidence_json": evidence_json_path,
        "report_md": report_md_path,
    }
