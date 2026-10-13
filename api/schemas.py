"""
Pydantic schemas for the FastAPI service wrapper.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from agent.state import (
    ApprovalDecision,
    ApprovalItem,
    ApprovalItemDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    ItemAction,
    RunStatus,
)


class CreateRunRequest(BaseModel):
    request_text: str = Field(..., description="High-level natural language request, e.g. 'Acme questionnaire'")
    run_id: Optional[str] = Field(None, description="Optional custom run_id")


class RunSummaryResponse(BaseModel):
    run_id: str
    status: str
    created_at: str
    request_text: str


class ResolveApprovalRequest(BaseModel):
    approval_id: str
    decisions: Optional[List[ApprovalItemDecision]] = None
    final_action: Optional[ItemAction] = None
    decided_by: str = "human_operator"


class HealthResponse(BaseModel):
    status: str
    version: str = "0.3.0"
