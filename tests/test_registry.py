import pytest
from pydantic import BaseModel, Field
from agent.state import ApprovalRequest, RiskLevel, RunState
from agent.tools.registry import ApprovalRequiredError, Tool, ToolRegistry


class DummyInput(BaseModel):
    message: str = Field(..., description="Message string")


class ReadDummyTool(Tool):
    name = "read_dummy"
    description = "Test read tool"
    risk = RiskLevel.READ
    args_schema = DummyInput

    def run(self, state: RunState, **kwargs):
        return {"result": f"read: {kwargs['message']}"}


class WriteHighDummyTool(Tool):
    name = "submit_action"
    description = "High risk submission action"
    risk = RiskLevel.WRITE_HIGH
    args_schema = DummyInput

    def run(self, state: RunState, **kwargs):
        return {"result": f"submitted: {kwargs['message']}"}


def test_registry_registration_and_export():
    registry = ToolRegistry()
    tool = ReadDummyTool()
    registry.register(tool)

    assert registry.get("read_dummy") == tool
    specs = registry.export_for_llm()
    assert len(specs) == 1
    assert specs[0]["function"]["name"] == "read_dummy"
    assert "[READ]" in specs[0]["function"]["description"]


def test_read_tool_executes_freely():
    registry = ToolRegistry()
    registry.register(ReadDummyTool())

    state = RunState(run_id="run-1", request_text="test")
    res = registry.execute("read_dummy", state, message="hello")
    assert res["result"] == "read: hello"


def test_write_high_tool_refuses_without_approval():
    registry = ToolRegistry()
    registry.register(WriteHighDummyTool())

    state = RunState(run_id="run-1", request_text="test")
    # No pending approval
    with pytest.raises(ApprovalRequiredError) as exc_info:
        registry.execute("submit_action", state, message="execute submission")
    assert "no pending approval exists" in str(exc_info.value)


def test_write_high_tool_refuses_when_unapproved():
    registry = ToolRegistry()
    registry.register(WriteHighDummyTool())

    state = RunState(run_id="run-1", request_text="test")
    state.pending_approval = ApprovalRequest(
        id="APP-001",
        reason="Needs human approval",
        approved=False,
    )

    with pytest.raises(ApprovalRequiredError) as exc_info:
        registry.execute("submit_action", state, message="execute submission")
    assert "has not been approved" in str(exc_info.value)


def test_write_high_tool_succeeds_when_approved():
    registry = ToolRegistry()
    registry.register(WriteHighDummyTool())

    state = RunState(run_id="run-1", request_text="test")
    state.pending_approval = ApprovalRequest(
        id="APP-001",
        reason="Needs human approval",
        approved=True,
        approved_by="sec-admin@company.com",
    )

    res = registry.execute("submit_action", state, message="execute submission")
    assert res["result"] == "submitted: execute submission"

test_registry_registration_and_export()
test_read_tool_executes_freely()
test_write_high_tool_refuses_without_approval()
test_write_high_tool_refuses_when_unapproved()
test_write_high_tool_succeeds_when_approved()