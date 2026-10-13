import os
import tempfile
import pytest
from agent.state import Answer, AnswerDecision, RunState
from agent.tools.files import FindInboxFilesTool, ParseQuestionnaireTool, WriteFilledXlsxTool


def test_files_tools_end_to_end():
    state = RunState(run_id="run-test", request_text="parse test")

    find_tool = FindInboxFilesTool()
    inbox_res = find_tool.run(state, inbox_dir="data/inbox")
    assert len(inbox_res["files"]) > 0

    target_xlsx = inbox_res["files"][0]

    parse_tool = ParseQuestionnaireTool()
    parse_res = parse_tool.run(state, file_path=target_xlsx)
    assert parse_res["question_count"] == 40
    assert len(state.questions) == 40

    # Ensure prompt injections were flagged in state
    assert len(state.injection_flags) >= 2

    # Test writing filled xlsx
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
        out_xlsx = tf.name

    try:
        sample_answers = [
            Answer(
                question_id="Q-01",
                text="MFA is enforced with FIDO2 WebAuthn.",
                confidence=1.0,
                decision=AnswerDecision.ANSWERED,
            ),
            Answer(
                question_id="Q-34",
                text="Escalated: Legal sign-off required.",
                confidence=0.9,
                decision=AnswerDecision.ESCALATED,
            ),
        ]
        state.answers = sample_answers

        write_tool = WriteFilledXlsxTool()
        write_res = write_tool.run(state, source_file=target_xlsx, output_file=out_xlsx)
        assert write_res["filled_count"] == 2
        assert os.path.exists(out_xlsx)
        assert os.path.getsize(out_xlsx) > 0
    finally:
        if os.path.exists(out_xlsx):
            os.remove(out_xlsx)

