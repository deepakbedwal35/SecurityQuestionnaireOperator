"""
Mock CRM and ticket tracking tools (lookup_customer, log_submission).
"""
from typing import Any, Dict
from pydantic import BaseModel, Field
from agent.state import RiskLevel, RunState
from agent.tools.registry import Tool


class LookupCustomerInput(BaseModel):
    customer_name: str = Field(..., description="Customer name, e.g. Acme")


class LookupCustomerOutput(BaseModel):
    customer_id: str
    name: str
    tier: str
    portal_url: str
    account_executive: str


class LookupCustomerTool(Tool):
    name = "lookup_customer"
    description = "Looks up customer metadata, assigned portal URL, and account details in internal CRM."
    risk = RiskLevel.READ
    args_schema = LookupCustomerInput
    output_schema = LookupCustomerOutput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        cname = kwargs["customer_name"].strip()
        # Stub response for mock CRM
        return {
            "customer_id": f"CRM-{hash(cname) % 10000:04d}",
            "name": cname,
            "tier": "Enterprise",
            "portal_url": "http://localhost:3000/portal/acme",
            "account_executive": "jane.doe@northwind.io",
        }


class LogSubmissionInput(BaseModel):
    customer_id: str
    submission_id: str
    status: str = "completed"
    artifact_path: str


class LogSubmissionOutput(BaseModel):
    crm_ticket_id: str
    logged: bool


class LogSubmissionTool(Tool):
    name = "log_submission"
    description = "Logs security questionnaire completion and artifact references to internal CRM ticket tracker."
    risk = RiskLevel.WRITE_LOW
    args_schema = LogSubmissionInput
    output_schema = LogSubmissionOutput

    def run(self, state: RunState, **kwargs: Any) -> Dict[str, Any]:
        return {
            "crm_ticket_id": f"TICK-{kwargs['submission_id']}",
            "logged": True,
        }

