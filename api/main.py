"""
Thin FastAPI application wrapper exposing OperatorService.
No business or graph execution logic lives in this layer.
"""
import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from agent.service import OperatorService, ValidationError
from agent.state import ApprovalDecision, RunState, RunStatus
from api.schemas import (
    CreateRunRequest,
    HealthResponse,
    ResolveApprovalRequest,
    RunSummaryResponse,
)

app = FastAPI(
    title="Security Questionnaire Operator API",
    description="Thin API exposing autonomous security questionnaire agent with human-in-the-loop approvals",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global service instance
_service: Optional[OperatorService] = None


def get_service() -> OperatorService:
    global _service
    if _service is None:
        _service = OperatorService()
    return _service


def set_service(svc: OperatorService) -> None:
    global _service
    _service = svc


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
def create_run(req: CreateRunRequest) -> Dict[str, Any]:
    svc = get_service()
    state = svc.start_run(request_text=req.request_text, run_id=req.run_id)
    return {
        "run_id": state.run_id,
        "status": state.status.value,
        "request_text": state.request_text,
    }


@app.get("/runs", response_model=List[RunSummaryResponse])
def list_runs() -> List[RunSummaryResponse]:
    svc = get_service()
    raw_runs = svc.list_runs()
    return [RunSummaryResponse(**r) for r in raw_runs]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    svc = get_service()
    state = svc.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return state.model_dump(mode="json")


@app.get("/runs/{run_id}/events")
def get_run_events(run_id: str, after_seq: int = Query(0, ge=0)) -> List[Dict[str, Any]]:
    svc = get_service()
    events = svc.get_events(run_id, after_seq=after_seq)
    return [e.model_dump() for e in events]


@app.get("/runs/{run_id}/events/stream")
async def stream_run_events(run_id: str, after_seq: int = Query(0, ge=0)) -> EventSourceResponse:
    svc = get_service()

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        last_seq = after_seq
        idle_cycles = 0

        while True:
            events = svc.get_events(run_id, after_seq=last_seq)
            if events:
                for ev in events:
                    last_seq = ev.seq
                    yield {
                        "event": ev.type,
                        "data": json.dumps(ev.model_dump(), default=str),
                    }
                idle_cycles = 0
            else:
                idle_cycles += 1

            # Check terminal state
            state = svc.get_run(run_id)
            if state and state.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.REJECTED_BY_HUMAN):
                if not svc.get_events(run_id, after_seq=last_seq):
                    # No new events remaining
                    break

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@app.get("/runs/{run_id}/approval")
def get_pending_approval(run_id: str) -> Dict[str, Any]:
    svc = get_service()
    approval = svc.get_pending_approval(run_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"No pending approval for run '{run_id}'")
    return approval.model_dump(mode="json")


@app.post("/runs/{run_id}/approvals/{approval_id}")
def resolve_approval(run_id: str, approval_id: str, req: ResolveApprovalRequest) -> Dict[str, Any]:
    svc = get_service()
    decision = ApprovalDecision(
        approval_id=req.approval_id or approval_id,
        decisions=req.decisions,
        final_action=req.final_action,
        decided_by=req.decided_by,
    )
    try:
        updated_state = svc.resolve_approval(run_id, approval_id, decision)
        return {
            "run_id": run_id,
            "status": updated_state.status.value,
            "approval_status": updated_state.pending_approval.status.value if updated_state.pending_approval else None,
        }
    except ValidationError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> Dict[str, Any]:
    svc = get_service()
    cancelled = svc.cancel_run(run_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail=f"Cannot cancel run '{run_id}' (already terminal or not found)")
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/runs/{run_id}/report")
def get_run_report(run_id: str) -> Dict[str, Any]:
    svc = get_service()
    report_data = svc.get_report(run_id)
    if "error" in report_data:
        raise HTTPException(status_code=404, detail=report_data["error"])
    return report_data
