"""
RunState and core data structures for Security Questionnaire Operator.
"""
from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"
    REJECTED_BY_HUMAN = "rejected_by_human"


class QuestionType(str, Enum):
    FACTUAL = "factual"
    COMMITMENT = "commitment"
    OUT_OF_SCOPE = "out_of_scope"


class AnswerDecision(str, Enum):
    ANSWERED = "answered"
    ESCALATED = "escalated"
    FLAGGED = "flagged"


class RiskLevel(str, Enum):
    READ = "READ"
    WRITE_LOW = "WRITE_LOW"
    WRITE_HIGH = "WRITE_HIGH"


class Citation(BaseModel):
    doc: str
    chunk_id: Optional[str] = None
    quote: str


class Question(BaseModel):
    id: str
    text: str
    type: QuestionType = QuestionType.FACTUAL
    raw_row: int
    flags: List[str] = Field(default_factory=list)


class Answer(BaseModel):
    question_id: str
    text: str
    citations: List[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    decision: AnswerDecision = AnswerDecision.ANSWERED
    conflict_note: Optional[str] = None
    human_override: Optional[str] = None


class PostconditionSpec(BaseModel):
    kind: str
    params: Dict[str, Any] = Field(default_factory=dict)


class PostconditionResult(BaseModel):
    kind: str
    passed: bool
    evidence: str
    detail: Optional[str] = None
    step_id: Optional[str] = None


class PlanStep(BaseModel):
    id: str
    description: str
    tool_hint: Optional[str] = None
    postcondition: PostconditionSpec
    status: str = "pending"  # pending | in_progress | completed | failed


class StepLog(BaseModel):
    ts: datetime = Field(default_factory=_utcnow)
    tool: str
    input: Dict[str, Any]
    output_summary: str
    risk: RiskLevel
    duration_ms: int = 0
    error: Optional[str] = None


class ApprovalKind(str, Enum):
    ANSWER_REVIEW = "ANSWER_REVIEW"
    FINAL_SIGNOFF = "FINAL_SIGNOFF"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class ItemAction(str, Enum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"


class ApprovalItem(BaseModel):
    question_id: str
    question_text: str
    draft_answer: Optional[str] = None
    evidence: List[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    flags: List[str] = Field(default_factory=list)
    reason_for_escalation: Optional[str] = None


class ApprovalItemDecision(BaseModel):
    question_id: str
    action: ItemAction
    edited_answer: Optional[str] = None
    note: Optional[str] = None


class ApprovalDecision(BaseModel):
    approval_id: str
    decisions: Optional[List[ApprovalItemDecision]] = None  # For ANSWER_REVIEW
    final_action: Optional[ItemAction] = None  # For FINAL_SIGNOFF: approve | reject
    decided_by: str = "human_operator"


class ApprovalRequest(BaseModel):
    id: str
    run_id: str = ""
    kind: ApprovalKind = ApprovalKind.ANSWER_REVIEW
    reason: str
    items: List[ApprovalItem] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.WRITE_HIGH
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=_utcnow)
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    approved_answers_hash: Optional[str] = None

    def __init__(self, **data: Any) -> None:
        if "approved" in data:
            approved_val = data.pop("approved")
            if "status" not in data:
                data["status"] = ApprovalStatus.APPROVED if approved_val else ApprovalStatus.PENDING
        super().__init__(**data)

    @property
    def approved(self) -> bool:
        return self.status == ApprovalStatus.APPROVED

    @approved.setter
    def approved(self, val: bool) -> None:
        self.status = ApprovalStatus.APPROVED if val else ApprovalStatus.REJECTED


class UnderstoodGoal(BaseModel):
    customer: Optional[str] = None
    questionnaire_path: Optional[str] = None
    selection_rationale: Optional[str] = None
    portal_url: Optional[str] = None
    deadline: Optional[str] = None
    constraints: List[str] = Field(default_factory=list)
    low_confidence_goal: bool = False
    needs_human_clarification: bool = False


class PortalState(BaseModel):
    logged_in: bool = False
    session_id: Optional[str] = None
    submission_id: Optional[str] = None
    last_snapshot_hash: Optional[str] = None
    current_url: Optional[str] = None
    storage_state_path: Optional[str] = None
    already_submitted: bool = False


class Counters(BaseModel):
    replan_count: int = 0
    tool_error_count: int = 0
    step_count: int = 0


class InjectionFlag(BaseModel):
    source: str
    snippet: str
    action_taken: str


class RunState(BaseModel):
    run_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    status: RunStatus = RunStatus.PENDING
    request_text: str
    understood_goal: Optional[UnderstoodGoal] = None
    plan: List[PlanStep] = Field(default_factory=list)
    plan_version: int = 1
    plan_history: List[List[PlanStep]] = Field(default_factory=list)
    questions: List[Question] = Field(default_factory=list)
    answers: List[Answer] = Field(default_factory=list)
    observations: List[StepLog] = Field(default_factory=list)
    pending_approval: Optional[ApprovalRequest] = None
    portal_state: PortalState = Field(default_factory=PortalState)
    counters: Counters = Field(default_factory=Counters)
    artifacts: Dict[str, str] = Field(default_factory=dict)
    injection_flags: List[InjectionFlag] = Field(default_factory=list)
    postcondition_results: List[PostconditionResult] = Field(default_factory=list)
    failure_reason: Optional[str] = None

    def add_injection_flag(self, source: str, snippet: str, action_taken: str) -> bool:
        """
        Deduplicates injection flags by (source, sha1(snippet)).
        Returns True if added, False if duplicate.
        """
        snip_hash = hashlib.sha1(snippet.encode("utf-8")).hexdigest()
        for existing in self.injection_flags:
            existing_snip = existing["snippet"] if isinstance(existing, dict) else existing.snippet
            existing_src = existing["source"] if isinstance(existing, dict) else existing.source
            if existing_src == source and hashlib.sha1(existing_snip.encode("utf-8")).hexdigest() == snip_hash:
                return False

        self.injection_flags.append(InjectionFlag(
            source=source,
            snippet=snippet,
            action_taken=action_taken,
        ))
        return True
