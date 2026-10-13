"""
Mock internal CRM and ticket tracking microservice.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock CRM Service", version="1.0.0")

CUSTOMERS = {
    "acme": {
        "customer_id": "CRM-0042",
        "name": "Acme Corp",
        "tier": "Enterprise",
        "portal_url": "http://localhost:3000/portal/acme",
        "account_executive": "alice@northwind.io",
    }
}

SUBMISSIONS = {}


class LogSubmissionPayload(BaseModel):
    customer_id: str
    submission_id: str
    status: str
    artifact_path: str


@app.get("/customers/{customer_name}")
def lookup_customer(customer_name: str):
    key = customer_name.lower().strip()
    if key in CUSTOMERS:
        return CUSTOMERS[key]
    return {
        "customer_id": f"CRM-{abs(hash(key)) % 1000:04d}",
        "name": customer_name,
        "tier": "Standard",
        "portal_url": f"http://localhost:3000/portal/{key}",
        "account_executive": "sales@northwind.io",
    }


@app.post("/submissions")
def log_submission(payload: LogSubmissionPayload):
    ticket_id = f"TICKET-{payload.submission_id}"
    SUBMISSIONS[ticket_id] = payload.model_dump()
    return {"ticket_id": ticket_id, "status": "logged"}

