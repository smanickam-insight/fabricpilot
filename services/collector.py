"""Bounded delegated inventory collection; unavailable deep telemetry stays absent."""

from datetime import datetime, timezone
from uuid import UUID

from services.access import Action, Resource, authorize
from services.identity import _collection
from services.fabric_api import failure_message
from services.redaction import redact_text


def collect_inventory(principal, credential):
    records = []
    observed = datetime.now(timezone.utc).isoformat()
    for grant in principal.workspaces:
        authorize(principal, Action.READ, Resource(principal.tenant_id, grant.workspace_id))
        workspace_id = str(UUID(grant.workspace_id))
        common = {"tenant_id": principal.tenant_id, "workspace_id": workspace_id,
                  "capacity_id": grant.capacity_id, "source": "fabric-rest", "observed_at": observed}
        records.append(common | {"id": workspace_id, "name": grant.name, "kind": "Workspace",
                                 "level": "workspace", "evidence": {"capacity_assigned": bool(grant.capacity_id)}})
        for item in _collection(credential, f"/workspaces/{workspace_id}/items"):
            item_id = str(UUID(item["id"]))
            kind = item.get("type", "Unknown")
            evidence = {}
            if kind in {"DataPipeline", "Notebook", "SparkJobDefinition"}:
                jobs = _collection(credential, f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances")
                if jobs:
                    latest = max(jobs, key=lambda job: job.get("startTimeUtc") or "")
                    evidence = {"run_id": latest["id"], "status": latest.get("status", "Unknown"),
                                "job_type": latest.get("jobType", ""), "error": redact_text(failure_message(latest))}
                    if latest.get("startTimeUtc"):
                        start = datetime.fromisoformat(latest["startTimeUtc"].replace("Z", "+00:00"))
                        end = datetime.fromisoformat(latest["endTimeUtc"].replace("Z", "+00:00")) if latest.get("endTimeUtc") else datetime.now(timezone.utc)
                        evidence["elapsed_seconds"] = max(0, (end - start).total_seconds())
            records.append(common | {"id": item_id, "name": item.get("displayName", item_id),
                                     "kind": kind, "level": "item", "evidence": evidence})
    return records