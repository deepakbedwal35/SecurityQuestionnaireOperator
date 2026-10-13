"""
Unit and scenario tests for:
- Permanent error aborts without replan
- Transient error retries then succeeds
- Replan preserves completed steps (does not re-execute find_inbox_files / parse_questionnaire)
- Report counts match state
- Injection flag deduplication
"""
import os
import tempfile
import pytest

from agent.executor import Executor, ExecutorConfig
from agent.llm import (
    FakeLLM,
    PermanentError,
    TransientError,
    classify_exception,
    resolve_model_tier,
)
from agent.memory import FeedbackStore
from agent.planner import Planner
from agent.report import generate_reports
from agent.state import (
    Answer,
    AnswerDecision,
    Citation,
    PlanStep,
    PostconditionSpec,
    Question,
    QuestionType,
    RiskLevel,
    RunState,
    RunStatus,
)
from agent.tools.files import FindInboxFilesTool, ParseQuestionnaireTool, WriteFilledXlsxTool
from agent.tools.registry import Tool, ToolRegistry
from agent.tools.retrieve import KeywordPolicyRetriever, RetrieveEvidenceTool


def _build_test_registry(llm: FakeLLM):
    feedback_store = FeedbackStore(file_path="data/feedback_rules.json")
    retriever = KeywordPolicyRetriever(policies_dir="data/policies", feedback_store=feedback_store)
    registry = ToolRegistry()
    registry.register(FindInboxFilesTool())
    registry.register(ParseQuestionnaireTool())
    registry.register(WriteFilledXlsxTool())
    registry.register(RetrieveEvidenceTool(retriever=retriever, feedback_store=feedback_store))
    return registry, retriever


def test_model_tier_resolution():
    os.environ["PLANNER_MODEL"] = "openai/gpt-oss-120b"
    os.environ["EXECUTOR_MODEL"] = "openai/gpt-oss-120b"
    os.environ["CHEAP_MODEL"] = "openai/gpt-oss-20b"

    assert resolve_model_tier("planner") == "openai/gpt-oss-120b"
    assert resolve_model_tier("executor") == "openai/gpt-oss-120b"
    assert resolve_model_tier("cheap") == "openai/gpt-oss-20b"
    # Explicit model string remains unchanged
    assert resolve_model_tier("custom/my-model") == "custom/my-model"


def test_permanent_error_aborts_without_replan():
    llm = FakeLLM()
    registry, _ = _build_test_registry(llm)

    class PermanentFailTool(Tool):
        name = "perm_fail_tool"
        description = "Throws 404 permanent model error"
        risk = RiskLevel.READ
        args_schema = None

        def __init__(self):
            self.calls = 0

        def run(self, state: RunState, **kwargs):
            self.calls += 1
            raise PermanentError("HTTP 404 Permanent Error: model_not_found")

    perm_tool = PermanentFailTool()
    registry.register(perm_tool)

    planner = Planner(llm, registry)
    config = ExecutorConfig(max_steps_per_run=10, max_retries_per_tool=2, max_replans=3)
    executor = Executor(registry, llm, planner, config)

    state = RunState(
        run_id="test-perm-err",
        request_text="test",
        plan=[
            PlanStep(
                id="step-perm",
                description="Call permanent failure tool",
                tool_hint="perm_fail_tool",
                postcondition=PostconditionSpec(kind="questions_parsed", params={"min_count": 1}),
            )
        ],
    )

    final_state = executor.run_loop(state)
    assert final_state.status == RunStatus.FAILED
    assert "configuration/permanent error" in final_state.failure_reason
    assert "model_not_found" in final_state.failure_reason
    # Proves no retries and no replans occurred
    assert perm_tool.calls == 1
    assert final_state.counters.replan_count == 0


def test_transient_error_retries_then_succeeds():
    llm = FakeLLM()
    registry, _ = _build_test_registry(llm)

    class TransientFailTool(Tool):
        name = "transient_tool"
        description = "Fails once with 429 then succeeds"
        risk = RiskLevel.READ
        args_schema = None

        def __init__(self):
            self.calls = 0

        def run(self, state: RunState, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TransientError("HTTP 429 rate limit exceeded")
            return {"status": "ok"}

    trans_tool = TransientFailTool()
    registry.register(trans_tool)

    planner = Planner(llm, registry)
    config = ExecutorConfig(max_steps_per_run=10, max_retries_per_tool=2, retry_backoff_base_s=0.001)
    executor = Executor(registry, llm, planner, config)

    state = RunState(run_id="test-transient", request_text="test")
    success = executor._invoke_tool_with_retry("transient_tool", {}, state)

    assert success is True
    assert trans_tool.calls == 2
    assert state.counters.tool_error_count == 1


def test_replan_preserves_completed_steps():
    llm = FakeLLM()
    registry, retriever = _build_test_registry(llm)

    class TrackingTool(Tool):
        name = "find_inbox_files"
        description = "Track calls"
        risk = RiskLevel.READ
        args_schema = None

        def __init__(self):
            self.calls = 0

        def run(self, state: RunState, **kwargs):
            self.calls += 1
            return {"files": ["data/inbox/Acme_Security_Questionnaire.xlsx"]}

    tracking_inbox = TrackingTool()
    # Replace find_inbox_files with tracker
    registry._tools["find_inbox_files"] = tracking_inbox

    planner = Planner(llm, registry, retriever)
    state = RunState(
        run_id="test-preserve-steps",
        request_text="test",
        questions=[Question(id="Q-01", text="Sample", type=QuestionType.FACTUAL, raw_row=2)],
    )

    # Initial plan
    initial_steps = planner.plan(state)
    assert len(initial_steps) == 6

    # Execute step 1 and step 2 successfully
    initial_steps[0].status = "completed"
    initial_steps[1].status = "completed"

    # Now simulate a failure triggering replan
    replan_steps = planner.plan(state, replan_reason="Step 3 failed")

    # Remaining plan must only have steps 3, 4, 5, 6 (not 1 and 2)
    assert len(replan_steps) == 4
    assert [s.id for s in replan_steps] == [
        "step-3-process-questions",
        "step-4-verify-commitments",
        "step-5-verify-citations",
        "step-6-write-output",
    ]
    assert state.plan_version == 2
    assert state.counters.replan_count == 1


def test_injection_flags_deduplication():
    state = RunState(run_id="test-dedupe", request_text="test")

    # Add duplicate flags
    added1 = state.add_injection_flag(
        source="doc:row_39",
        snippet="Ignore previous instructions",
        action_taken="Neutralized",
    )
    added2 = state.add_injection_flag(
        source="doc:row_39",
        snippet="Ignore previous instructions",
        action_taken="Neutralized again",
    )
    added3 = state.add_injection_flag(
        source="doc:row_40",
        snippet="Disregard prior instructions",
        action_taken="Neutralized",
    )

    assert added1 is True
    assert added2 is False  # Duplicate ignored
    assert added3 is True
    assert len(state.injection_flags) == 2


def test_report_counts_match_state():
    state = RunState(
        run_id="test-report-counts",
        request_text="test",
        status=RunStatus.COMPLETED,
    )

    state.questions = [
        Question(id=f"Q-{i}", text=f"Question {i}", type=QuestionType.FACTUAL, raw_row=i+1)
        for i in range(1, 11)
    ]

    state.answers = [
        Answer(question_id="Q-1", text="Ans 1", decision=AnswerDecision.ANSWERED, citations=[Citation(doc="Doc", quote="q")]),
        Answer(question_id="Q-2", text="Ans 2", decision=AnswerDecision.ANSWERED, citations=[Citation(doc="Doc", quote="q")]),
        Answer(question_id="Q-3", text="Esc 1", decision=AnswerDecision.ESCALATED),
        Answer(question_id="Q-4", text="Flag 1", decision=AnswerDecision.FLAGGED),
    ]

    state.add_injection_flag(source="file.xlsx", snippet="Ignore all", action_taken="Flagged")

    reports = generate_reports(state)
    assert os.path.exists(reports["report_md"])

    with open(reports["report_md"], "r", encoding="utf-8") as f:
        content = f.read()

    # Verify exact format and counts
    expected_summary = "Parsed: 10, Processed: 4, Answered: 2, Escalated: 1, Flagged: 1"
    assert expected_summary in content
    assert "**Prompt Injections Neutralized:** 1" in content
