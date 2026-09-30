"""Fabric Pilot API. Run with uvicorn api:app --host 127.0.0.1 --port 8000."""

from dataclasses import asdict
from functools import lru_cache
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from services.access import AccessDenied
from services.agent_brain import diagnose
from services.assessment import assessments, incidents, scorecard
from services.collector import collect_inventory
from services.identity import EntraValidator, delegated_credential, discover_principal
from services.operations import OperationStore, execute_plan
from services.telemetry import collect_kql


app = FastAPI(title="Fabric Pilot API", version="0.1.0")
bearer = HTTPBearer()


@lru_cache(maxsize=1)
def store():
    return OperationStore(os.getenv("PILOT_DATABASE", str(Path(__file__).parent / ".pilot" / "pilot.db")))


@lru_cache(maxsize=1)
def validator():
    return EntraValidator(os.getenv("PILOT_API_AUDIENCE", ""),
                          [tenant.strip() for tenant in os.getenv("PILOT_ALLOWED_TENANTS", "").split(",") if tenant.strip()])


def identity(authorization: HTTPAuthorizationCredentials = Depends(bearer)):
    if not os.getenv("PILOT_CLIENT_ID") or not os.getenv("PILOT_CLIENT_SECRET"):
        raise HTTPException(503, "Entra API application is not configured.")
    try:
        claims, credential = delegated_credential(authorization.credentials, validator(),
                                                  os.environ["PILOT_CLIENT_ID"], os.environ["PILOT_CLIENT_SECRET"])
        with credential:
            yield discover_principal(claims, credential), credential
    except AccessDenied:
        raise
    except Exception as exc:
        raise HTTPException(503, "Identity or delegated permission discovery is unavailable.") from exc


@app.exception_handler(AccessDenied)
def access_denied(_request: Request, exc: AccessDenied):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def invalid_action(_request: Request, _exc: ValueError):
    return JSONResponse(status_code=409, content={"detail": "Invalid action or action state conflict."})


@app.get("/healthz")
def health():
    return {"status": "ok", "execution_enabled": os.getenv("PILOT_ENABLE_EXECUTION") == "true"}


@app.get("/v1/me")
def me(context=Depends(identity)):
    principal, _credential = context
    return {"tenant_id": principal.tenant_id, "object_id": principal.object_id,
            "name": principal.display_name, "roles": principal.role_labels,
            "workspaces": [asdict(grant) for grant in principal.workspaces]}


@app.post("/v1/collect")
def collect(context=Depends(identity)):
    principal, credential = context
    records = collect_inventory(principal, credential)
    if os.getenv("PILOT_KQL_CLUSTER"):
        telemetry = collect_kql(principal, credential, os.environ["PILOT_KQL_CLUSTER"],
                                os.getenv("PILOT_KQL_DATABASE", ""),
                                {tenant.strip() for tenant in os.getenv("PILOT_KQL_ALLOWED_TENANTS", "").split(",")})
        records = list({record["id"]: record for record in records + telemetry}.values())
    store().put_records(principal, records)
    return {"records_collected": len(records), "source": "fabric-rest"}


@app.get("/v1/assessments")
def assessment_results(context=Depends(identity)):
    principal, _credential = context
    results = assessments(principal, store().records(principal))
    return {"scorecard": scorecard(results), "checks": results}


@app.get("/v1/incidents")
def incident_results(context=Depends(identity)):
    principal, _credential = context
    return incidents(principal, store().records(principal))


@app.post("/v1/diagnostics/{record_id}")
def diagnosis(record_id: str, context=Depends(identity)):
    principal, _credential = context
    record = next((record for record in store().records(principal) if record["id"] == record_id), None)
    if record is None:
        raise HTTPException(404, "Resource not found in the authorized scope.")
    return diagnose(principal, record)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: str


@app.post("/v1/plans")
def create_plan(request: PlanRequest, context=Depends(identity)):
    principal, _credential = context
    records = store().records(principal)
    incident = next((item for item in incidents(principal, records) if item["id"] == request.incident_id), None)
    if incident is None:
        raise HTTPException(404, "Incident not found in the authorized scope.")
    record = next(record for record in records if record["id"] == incident["record_id"])
    return {"plan_id": store().create_plan(principal, incident, record)}


@app.post("/v1/plans/{plan_id}/approve")
def approve(plan_id: str, context=Depends(identity)):
    principal, _credential = context
    store().approve(principal, plan_id)
    return {"status": "Approved"}


@app.post("/v1/plans/{plan_id}/execute")
def execute(plan_id: str, context=Depends(identity)):
    principal, credential = context
    return execute_plan(store(), principal, plan_id, credential, enabled=os.getenv("PILOT_ENABLE_EXECUTION") == "true")


@app.get("/v1/audit")
def audit(context=Depends(identity)):
    principal, _credential = context
    return store().audit(principal)