"""
Human escalation and approval tool (triggers interrupt_before in LangGraph).
"""
import uuid
from typing import Any, Dict, List
from pydantic import BaseModel, Field
from agent.state import ApprovalRequest, RiskLevel, RunState, RunStatus
from agent.tools.registry import Tool


class RequestHumanInput(BaseModel):
    reason: str = Field(..., description="Why human intervention or sign-off is required")
    items: List[Dict[str, Any]] = Field(default_factory=list, description="Items needing review")


class RequestHumanOutput(BaseModel):
    approval_id: str
    status: str


class RequestHumanTool(Tool):
    name = "request_human_input"
    description = "Requests human review or sign-off for high-risk actions, ungrounded answers, or legal commitments."
    risk = RiskLevel.READ
    args_schema = RequestHumanInput
    output_schema = RequestHumanOutput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        approval_id = f"APP-{uuid.uuid4().hex[:8]}"
        state.pending_approval = ApprovalRequest(
            id=approval_id,
            reason=kwargs["reason"],
            items=kwargs.get("items", []),
            risk=RiskLevel.WRITE_HIGH,
            approved=False,
        )
        state.status = RunStatus.WAITING_APPROVAL
        return {
            "approval_id": approval_id,
            "status": "waiting_approval",
        }

