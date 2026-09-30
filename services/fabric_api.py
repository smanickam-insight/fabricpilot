"""
services/fabric_api.py

Thin REST client over the **Microsoft Fabric REST API**
(https://api.fabric.microsoft.com/v1), using the SAME Microsoft Entra ID
interactive sign-in as the ADF views — only the token scope differs
(see services.auth.FABRIC_SCOPE).

Same contract as the ADF/ARM clients so the UI layer never handles exceptions:
    {"value": [...], "error": None, "pages_fetched": n, "truncated": bool}

Read-only by design: only GET endpoints are used here. Nothing in this module
can create, run, cancel or delete a Fabric item.

Endpoints used
--------------
    GET /workspaces
    GET /workspaces/{workspaceId}/items
    GET /workspaces/{workspaceId}/items/{itemId}/jobs/instances
    GET /workspaces/{workspaceId}/items/{itemId}/jobs/instances/{jobInstanceId}
    GET /capacities

Notes
-----
* Fabric paginates with `continuationToken` / `continuationUri` rather than
  ARM's `nextLink`, so `_get_paged` handles both.
* Item "kinds" of interest: DataPipeline, Notebook, Lakehouse, Warehouse,
  SemanticModel, SparkJobDefinition, Dataflow.
* Job statuses: NotStarted, InProgress, Completed, Failed, Cancelled, Deduped.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import requests

from services.auth import FABRIC_BASE, fabric_headers
from services.redaction import redact_text

MAX_PAGES = 20
REQUEST_TIMEOUT = 60

PIPELINE_KINDS = {"DataPipeline"}
NOTEBOOK_KINDS = {"Notebook", "SparkJobDefinition"}
MONITORABLE_KINDS = PIPELINE_KINDS | NOTEBOOK_KINDS | {"Dataflow", "SemanticModel"}

FAILED_STATES = {"Failed", "Cancelled", "Deduped"}


# ---------------------------------------------------------------- internals
def _ok(value, pages=1, truncated=False, **extra):
    out = {"value": value, "error": None, "pages_fetched": pages, "truncated": truncated}
    out.update(extra)
    return out


def _err(message):
    return {"value": [], "error": redact_text(str(message)),
            "pages_fetched": 0, "truncated": False}


def _describe(resp: requests.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error", body)
        return redact_text(
            f"{err.get('errorCode', err.get('code', resp.status_code))}: "
            f"{err.get('message', resp.text[:400])}"
        )
    except Exception:  # noqa: BLE001
        return redact_text(f"{resp.status_code}: {resp.text[:400]}")


def _get_paged(path: str, params: dict | None = None):
    """
    GET a Fabric collection, following continuationToken/continuationUri.
    `path` is relative to FABRIC_BASE, e.g. "/workspaces".
    """
    try:
        headers = fabric_headers()
        url = f"{FABRIC_BASE}{path}"
        items, pages, token, next_uri = [], 0, None, None

        while pages < MAX_PAGES:
            if next_uri:
                resp = requests.get(next_uri, headers=headers, timeout=REQUEST_TIMEOUT)
            else:
                query = dict(params or {})
                if token:
                    query["continuationToken"] = token
                resp = requests.get(url, headers=headers, params=query, timeout=REQUEST_TIMEOUT)

            if resp.status_code != 200:
                return _err(_describe(resp))

            body = resp.json()
            items.extend(body.get("value", []))
            pages += 1

            next_uri = body.get("continuationUri")
            token = body.get("continuationToken")
            if not next_uri and not token:
                break

        return _ok(items, pages, truncated=bool(next_uri or token))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# ------------------------------------------------------------ public API
def list_workspaces():
    """Every Fabric workspace the signed-in user can access."""
    return _get_paged("/workspaces")


def list_capacities():
    """Fabric capacities visible to the signed-in user (SKU, state, region)."""
    return _get_paged("/capacities")


def list_items(workspace_id: str, item_type: str | None = None):
    """
    All items in a workspace. Optionally filter server-side by type,
    e.g. item_type="DataPipeline" or "Notebook".
    """
    params = {"type": item_type} if item_type else None
    return _get_paged(f"/workspaces/{workspace_id}/items", params)


def list_data_pipelines(workspace_id: str):
    return list_items(workspace_id, "DataPipeline")


def list_notebooks(workspace_id: str):
    return list_items(workspace_id, "Notebook")


def list_job_instances(workspace_id: str, item_id: str):
    """Run history (job instances) for a pipeline / notebook / Spark job."""
    return _get_paged(f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances")


def get_job_instance(workspace_id: str, item_id: str, job_instance_id: str):
    """Full detail for a single job instance (includes failureReason)."""
    try:
        resp = requests.get(
            f"{FABRIC_BASE}/workspaces/{workspace_id}/items/{item_id}"
            f"/jobs/instances/{job_instance_id}",
            headers=fabric_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return _err(_describe(resp))
        return _ok([resp.json()])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# ------------------------------------------------------------ helpers
def _parse_iso(value):
    if not value:
        return None
    try:
        v = str(value).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(v)
    except Exception:  # noqa: BLE001
        return None


def duration_seconds(job: dict):
    start = _parse_iso(job.get("startTimeUtc"))
    end = _parse_iso(job.get("endTimeUtc"))
    if not start or not end:
        return None
    return round((end - start).total_seconds(), 1)


def failure_message(job: dict) -> str:
    """Flatten Fabric's nested failureReason into a single readable string."""
    reason = job.get("failureReason") or {}
    if isinstance(reason, str):
        return reason
    parts = [
        str(reason.get("errorCode") or ""),
        str(reason.get("message") or ""),
    ]
    inner = reason.get("moreDetails") or []
    if isinstance(inner, list):
        for d in inner:
            if isinstance(d, dict) and d.get("message"):
                parts.append(str(d["message"]))
    text = ": ".join(p for p in parts if p).strip(": ")
    return text or ""


def jobs_to_dataframe(jobs: list[dict], item_name: str = "", item_kind: str = "") -> pd.DataFrame:
    """Flatten job instances into a display-ready DataFrame."""
    rows = []
    for j in jobs or []:
        rows.append({
            "Item": item_name or j.get("itemId", ""),
            "Kind": item_kind,
            "Status": j.get("status", ""),
            "Job Type": j.get("jobType", ""),
            "Invoke Type": (j.get("invokeType") or ""),
            "Start (UTC)": (j.get("startTimeUtc") or "")[:19].replace("T", " "),
            "End (UTC)": (j.get("endTimeUtc") or "")[:19].replace("T", " "),
            "Duration (s)": duration_seconds(j),
            "Error": failure_message(j),
            "Job Instance Id": j.get("id", ""),
        })
    return pd.DataFrame(rows)


def jobs_to_analytics_frame(jobs: list[dict], item_name: str, item_kind: str) -> pd.DataFrame:
    """
    Normalise Fabric job instances into the EXACT schema expected by
    services.analytics.analyze() so the Fabric tab can reuse the whole
    ADF analytics + remediation + alerting engine.
    """
    rows = []
    for j in jobs or []:
        if j.get("status") not in FAILED_STATES:
            continue
        rows.append({
            "TimeGenerated": _parse_iso(j.get("startTimeUtc")) or dt.datetime.now(dt.timezone.utc),
            "PipelineName": item_name,
            "ActivityName": j.get("jobType") or item_kind or "Job",
            "ActivityType": "FabricNotebook" if item_kind in NOTEBOOK_KINDS else "FabricPipeline",
            "Status": "Failed",
            "Error": failure_message(j) or "Unknown error",
            "PipelineRunId": j.get("id", ""),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["TimeGenerated"] = pd.to_datetime(df["TimeGenerated"], utc=True).dt.tz_localize(None)
    return df
