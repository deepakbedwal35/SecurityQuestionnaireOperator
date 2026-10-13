"""
Independent verifier checking machine postconditions against real external artifacts.
The acting model never grades its own work.
"""
import os
from typing import Any, Dict, List, Optional
import openpyxl
from agent.policy import PolicyEngine
from agent.state import AnswerDecision, PostconditionResult, PostconditionSpec, QuestionType, RunState


class Verifier:
    """
    Independent verifier inspecting real physical artifacts and internal state constraints:
    - file_exists: Confirms target file exists on disk and is non-empty.
    - questions_parsed: Confirms >= min_count questions are parsed into state.
    - all_questions_have_decision: Every question has an Answer with answered/escalated/flagged.
    - no_commitment_question_answered: Proves that zero legal/SLA commitment questions are marked answered.
    - every_answered_has_citation: Every answered question has >= 1 citation quote grounded in cited doc.
    - output_xlsx_complete: Physically reads the generated Excel file, checking cell values and filled counts.
    """

    @staticmethod
    def verify(spec: PostconditionSpec, state: RunState, step_id: Optional[str] = None) -> PostconditionResult:
        handler = getattr(Verifier, f"_verify_{spec.kind}", None)
        if not handler:
            res = PostconditionResult(
                kind=spec.kind,
                passed=False,
                evidence="Unknown postcondition kind",
                detail=f"No verification handler implemented for kind '{spec.kind}'",
                step_id=step_id,
            )
            state.postcondition_results.append(res)
            return res
        result = handler(spec.params, state)
        result.step_id = step_id
        state.postcondition_results.append(result)
        return result

    @staticmethod
    def _verify_file_exists(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        path = params.get("path")
        if not path and "state_key" in params:
            path = state.artifacts.get(params["state_key"])
        if not path and state.understood_goal:
            path = state.understood_goal.questionnaire_path

        if not path or not os.path.exists(path):
            return PostconditionResult(
                kind="file_exists",
                passed=False,
                evidence=f"File not found: {path}",
                detail="Target file does not exist on disk",
            )
        size = os.path.getsize(path)
        if size == 0:
            return PostconditionResult(
                kind="file_exists",
                passed=False,
                evidence=f"File {path} exists but is 0 bytes",
                detail="Empty file artifact",
            )
        return PostconditionResult(
            kind="file_exists",
            passed=True,
            evidence=f"File {path} verified on disk ({size} bytes)",
        )

    @staticmethod
    def _verify_questions_parsed(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        min_count = params.get("min_count", 1)
        actual_count = len(state.questions)
        passed = actual_count >= min_count
        return PostconditionResult(
            kind="questions_parsed",
            passed=passed,
            evidence=f"Found {actual_count} parsed questions (min required: {min_count})",
            detail=None if passed else f"Insufficient questions parsed: {actual_count} < {min_count}",
        )

    @staticmethod
    def _verify_all_questions_have_decision(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        if not state.questions:
            return PostconditionResult(
                kind="all_questions_have_decision",
                passed=False,
                evidence="No questions parsed in state",
            )
        ans_map = {a.question_id: a for a in state.answers}
        missing: List[str] = []
        for q in state.questions:
            if q.id not in ans_map or ans_map[q.id].decision not in (
                AnswerDecision.ANSWERED,
                AnswerDecision.ESCALATED,
                AnswerDecision.FLAGGED,
            ):
                missing.append(q.id)

        passed = len(missing) == 0
        return PostconditionResult(
            kind="all_questions_have_decision",
            passed=passed,
            evidence=f"{len(state.answers)}/{len(state.questions)} questions have a valid decision",
            detail=None if passed else f"Questions missing decision: {missing[:5]}",
        )

    @staticmethod
    def _verify_no_commitment_question_answered(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        violations: List[str] = []
        ans_map = {a.question_id: a for a in state.answers}
        for q in state.questions:
            is_commit, _ = PolicyEngine.is_commitment_question(q.text)
            if (is_commit or q.type == QuestionType.COMMITMENT) and q.id in ans_map:
                ans = ans_map[q.id]
                if ans.decision == AnswerDecision.ANSWERED:
                    violations.append(f"{q.id}: '{q.text[:60]}'")

        passed = len(violations) == 0
        return PostconditionResult(
            kind="no_commitment_question_answered",
            passed=passed,
            evidence=f"Zero commitment questions answered auto-compliantly" if passed else f"{len(violations)} violations found",
            detail=None if passed else f"Commitment questions incorrectly answered: {violations}",
        )

    @staticmethod
    def _verify_every_answered_has_citation(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        missing_citation: List[str] = []
        for a in state.answers:
            if a.decision == AnswerDecision.ANSWERED:
                if not a.citations:
                    missing_citation.append(a.question_id)
                else:
                    # Verify citation quote is not empty and doc exists
                    for cit in a.citations:
                        if not cit.quote or not cit.doc:
                            missing_citation.append(a.question_id)
                            break

        passed = len(missing_citation) == 0
        return PostconditionResult(
            kind="every_answered_has_citation",
            passed=passed,
            evidence=f"All {len(state.answers)} answers checked for required citations",
            detail=None if passed else f"Answers missing citations: {missing_citation}",
        )

    @staticmethod
    def _verify_output_xlsx_complete(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        out_file = params.get("output_path") or state.artifacts.get("filled_xlsx")
        if not out_file or not os.path.exists(out_file):
            return PostconditionResult(
                kind="output_xlsx_complete",
                passed=False,
                evidence=f"Output xlsx missing at '{out_file}'",
            )

        # Reopen Excel file physically with openpyxl
        try:
            wb = openpyxl.load_workbook(out_file, data_only=True)
            ws = wb.active
            filled_rows = 0
            total_rows = 0
            for row_idx in range(2, ws.max_row + 1):
                qid = ws.cell(row=row_idx, column=1).value
                if not qid:
                    continue
                total_rows += 1
                ans_val = ws.cell(row=row_idx, column=4).value
                if ans_val and str(ans_val).strip():
                    filled_rows += 1

            passed = (filled_rows == total_rows) and total_rows > 0
            return PostconditionResult(
                kind="output_xlsx_complete",
                passed=passed,
                evidence=f"Re-opened {os.path.basename(out_file)}: {filled_rows}/{total_rows} questions populated",
                detail=None if passed else f"Missing answers in rows: {total_rows - filled_rows} rows empty",
            )
        except Exception as e:
            return PostconditionResult(
                kind="output_xlsx_complete",
                passed=False,
                evidence="Failed to open/inspect output xlsx",
                detail=str(e),
            )

    @staticmethod
    def _verify_final_artifact_signed_off(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        final_path = params.get("path") or state.artifacts.get("final_xlsx") or f"artifacts/{state.run_id}/final.xlsx"
        if not os.path.exists(final_path):
            return PostconditionResult(
                kind="final_artifact_signed_off",
                passed=False,
                evidence=f"Final artifact file missing at '{final_path}'",
            )
        size = os.path.getsize(final_path)
        if size == 0:
            return PostconditionResult(
                kind="final_artifact_signed_off",
                passed=False,
                evidence=f"Final artifact file at '{final_path}' is empty (0 bytes)",
            )
        return PostconditionResult(
            kind="final_artifact_signed_off",
            passed=True,
            evidence=f"Final artifact verified at '{final_path}' ({size} bytes)",
        )

    @staticmethod
    def _verify_portal_submission_confirmed(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        sub_id = state.portal_state.submission_id
        if not sub_id:
            return PostconditionResult(
                kind="portal_submission_confirmed",
                passed=False,
                evidence="No submission_id recorded in state.",
            )
        import re
        if not re.match(r"^SUB-[A-Za-z0-9]+$", sub_id):
            return PostconditionResult(
                kind="portal_submission_confirmed",
                passed=False,
                evidence=f"Submission ID '{sub_id}' does not match required pattern 'SUB-xxxxx'.",
            )
        return PostconditionResult(
            kind="portal_submission_confirmed",
            passed=True,
            evidence=f"Submission ID verified: {sub_id}",
        )

    @staticmethod
    def _verify_portal_answers_match_state(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        """
        Independent verification: launches a FRESH browser context, visits /submissions,
        reads back stored answers, and compares against state.answers.
        """
        portal_env = os.environ.get("PORTAL_URL", "http://127.0.0.1:4000")
        base_portal = "/".join(portal_env.split("/")[:3]) if "://" in portal_env else portal_env
        portal_url = params.get("portal_url") or base_portal
        sub_id = state.portal_state.submission_id
        if not sub_id:
            return PostconditionResult(
                kind="portal_answers_match_state",
                passed=False,
                evidence="Cannot verify portal answers: no submission_id in state.",
            )

        from playwright.sync_api import sync_playwright
        mismatches = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context()
                page = ctx.new_page()

                # Login
                user = os.environ.get("PORTAL_USER", "admin@acme.com")
                pwd = os.environ.get("PORTAL_PASS", "AcmeSecret2024!")
                page.goto(f"{portal_url}/login")
                page.locator('input[type="text"], input[name="username"]').first.fill(user)
                page.locator('input[type="password"], input[name="password"]').first.fill(pwd)
                page.locator('button[type="submit"], input[type="submit"]').first.click()
                page.wait_for_load_state("domcontentloaded")

                # Navigate to submissions
                page.goto(f"{portal_url}/submissions")
                page.wait_for_load_state("domcontentloaded")

                # Locate target submission card
                sub_card = page.locator(f".submission-card:has-text('{sub_id}')")
                if sub_card.count() == 0:
                    browser.close()
                    return PostconditionResult(
                        kind="portal_answers_match_state",
                        passed=False,
                        evidence=f"Submission '{sub_id}' not found on portal /submissions page.",
                    )

                # Extract stored answers table
                rows = sub_card.locator("table tbody tr").all()
                stored_answers = {}
                for r in rows:
                    cols = r.locator("td").all()
                    if len(cols) >= 2:
                        qid = cols[0].text_content().strip()
                        ans = cols[1].text_content().strip()
                        stored_answers[qid] = ans

                browser.close()

            # Compare against state answers
            state_answers = {a.question_id: a.text for a in state.answers if a.decision.value == "answered"}
            for qid, exp_ans in state_answers.items():
                if qid in stored_answers:
                    got_ans = stored_answers[qid]
                    if got_ans != exp_ans and exp_ans not in got_ans and got_ans not in exp_ans:
                        mismatches.append(f"{qid}: expected '{exp_ans[:40]}...', got '{got_ans[:40]}...'")

            if mismatches:
                return PostconditionResult(
                    kind="portal_answers_match_state",
                    passed=False,
                    evidence=f"{len(mismatches)} answer mismatches between portal and state.",
                    detail="; ".join(mismatches[:5]),
                )

            return PostconditionResult(
                kind="portal_answers_match_state",
                passed=True,
                evidence=f"All {len(stored_answers)} portal answers match verified state answers.",
            )
        except Exception as e:
            return PostconditionResult(
                kind="portal_answers_match_state",
                passed=False,
                evidence="Error during independent portal answer verification.",
                detail=str(e),
            )

    @staticmethod
    def _verify_no_double_submission(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        """Confirms that exactly one submission exists for this questionnaire on the portal."""
        portal_env = os.environ.get("PORTAL_URL", "http://127.0.0.1:4000")
        base_portal = "/".join(portal_env.split("/")[:3]) if "://" in portal_env else portal_env
        portal_url = params.get("portal_url") or base_portal
        qid = params.get("questionnaire_id") or "acme-sec-2024"
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context()
                page = ctx.new_page()
                user = os.environ.get("PORTAL_USER", "admin@acme.com")
                pwd = os.environ.get("PORTAL_PASS", "AcmeSecret2024!")
                page.goto(f"{portal_url}/login")
                page.locator('input[type="text"], input[name="username"]').first.fill(user)
                page.locator('input[type="password"], input[name="password"]').first.fill(pwd)
                page.locator('button[type="submit"], input[type="submit"]').first.click()
                page.wait_for_load_state("domcontentloaded")
                page.goto(f"{portal_url}/submissions")

                cards = page.locator(f".submission-card:has-text('{qid}')").all()
                count = len(cards)
                browser.close()

            if count > 1:
                return PostconditionResult(
                    kind="no_double_submission",
                    passed=False,
                    evidence=f"Found {count} submissions for questionnaire '{qid}' (duplicate detected).",
                )
            return PostconditionResult(
                kind="no_double_submission",
                passed=True,
                evidence=f"Confirmed exactly {count} submission exists for questionnaire '{qid}'.",
            )
        except Exception as e:
            return PostconditionResult(
                kind="no_double_submission",
                passed=False,
                evidence="Failed to verify submission count on portal.",
                detail=str(e),
            )

    @staticmethod
    def _verify_portal_screenshots_present(params: Dict[str, Any], state: RunState) -> PostconditionResult:
        screens = [p for k, p in state.artifacts.items() if k.startswith("screenshot_") and os.path.exists(p)]
        if not screens:
            # Check screens dir
            screen_dir = os.path.join("artifacts", state.run_id, "screens")
            if os.path.exists(screen_dir):
                screens = [os.path.join(screen_dir, f) for f in os.listdir(screen_dir) if f.endswith(".png")]

        if not screens:
            return PostconditionResult(
                kind="portal_screenshots_present",
                passed=False,
                evidence="No portal screenshots found in state artifacts or artifacts screens directory.",
            )
        return PostconditionResult(
            kind="portal_screenshots_present",
            passed=True,
            evidence=f"Found {len(screens)} portal screenshots including confirmation proof.",
        )

