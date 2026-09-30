"""
Thin REST client over the Azure Resource Manager / Data Factory APIs.

Every function returns a dict shaped like the Fabric service layer:
    {"value": [...], "error": None, "pages_fetched": n, "truncated": bool}

Read-only by design: the two POST endpoints are query-with-body operations,
so this client cannot trigger, cancel or modify a pipeline.

Endpoints used (api-version 2018-06-01 for Data Factory):
    GET  /subscriptions
    GET  /subscriptions/{sub}/providers/Microsoft.DataFactory/factories
    GET  .../factories/{factory}/pipelines
    POST .../factories/{factory}/queryPipelineRuns
    GET  .../factories/{factory}/pipelineruns/{runId}
    POST .../factories/{factory}/pipelineruns/{runId}/queryActivityruns
"""

from __future__ import annotations

import requests

from services.auth import ARM_BASE, auth_headers
from services.redaction import redact_text

DF_API_VERSION = "2018-06-01"
ARM_API_VERSION = "2022-12-01"
MAX_PAGES = 20
REQUEST_TIMEOUT = 60


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
        return redact_text(f"{err.get('code', resp.status_code)}: "
                           f"{err.get('message', resp.text[:400])}")
    except Exception:  # noqa: BLE001
        return redact_text(f"{resp.status_code}: {resp.text[:400]}")


def _get_paged(url: str, params: dict | None = None):
    try:
        headers = auth_headers()
        items, pages, next_url = [], 0, url
        while next_url and pages < MAX_PAGES:
            resp = requests.get(
                next_url,
                headers=headers,
                params=params if pages == 0 else None,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return _err(_describe(resp))
            body = resp.json()
            items.extend(body.get("value", []))
            next_url = body.get("nextLink")
            pages += 1
        return _ok(items, pages, truncated=bool(next_url))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _post_paged(url: str, payload: dict):
    try:
        headers = auth_headers()
        items, pages, token = [], 0, None
        while pages < MAX_PAGES:
            body = dict(payload)
            if token:
                body["continuationToken"] = token
            resp = requests.post(
                url,
                headers=headers,
                json=body,
                params={"api-version": DF_API_VERSION},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return _err(_describe(resp))
            data = resp.json()
            items.extend(data.get("value", []))
            pages += 1
            token = data.get("continuationToken")
            if not token:
                break
        return _ok(items, pages, truncated=bool(token))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def parse_factory_id(factory_id: str) -> tuple[str, str, str]:
    """Split an ARM resource id into (subscription_id, resource_group, name)."""
    parts = factory_id.strip("/").split("/")
    lookup = {parts[i].lower(): parts[i + 1] for i in range(0, len(parts) - 1, 2)}
    return (lookup.get("subscriptions", ""),
            lookup.get("resourcegroups", ""),
            lookup.get("factories", ""))


def _factory_url(sub: str, rg: str, factory: str, suffix: str = "") -> str:
    return (
        f"{ARM_BASE}/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.DataFactory/factories/{factory}{suffix}"
    )


# ------------------------------------------------------------ public API
def list_tenants():
    return _get_paged(f"{ARM_BASE}/tenants", {"api-version": ARM_API_VERSION})


def list_subscriptions():
    result = _get_paged(f"{ARM_BASE}/subscriptions", {"api-version": ARM_API_VERSION})
    if result["error"]:
        return result
    enabled = [s for s in result["value"] if s.get("state") in (None, "Enabled", "Warned")]
    return _ok(enabled, result["pages_fetched"], result["truncated"])


def list_factories(subscription_id: str):
    return _get_paged(
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.DataFactory/factories",
        {"api-version": DF_API_VERSION},
    )


def list_pipelines(subscription_id: str, resource_group: str, factory_name: str):
    return _get_paged(
        _factory_url(subscription_id, resource_group, factory_name, "/pipelines"),
        {"api-version": DF_API_VERSION},
    )


def query_pipeline_runs(subscription_id: str, resource_group: str, factory_name: str,
                        pipeline_name: str, last_updated_after: str, last_updated_before: str):
    payload = {
        "lastUpdatedAfter": last_updated_after,
        "lastUpdatedBefore": last_updated_before,
        "filters": [
            {"operand": "PipelineName", "operator": "Equals", "values": [pipeline_name]}
        ],
        "orderBy": [{"orderBy": "RunStart", "order": "DESC"}],
    }
    return _post_paged(
        _factory_url(subscription_id, resource_group, factory_name, "/queryPipelineRuns"),
        payload,
    )


def get_pipeline_run(subscription_id: str, resource_group: str, factory_name: str, run_id: str):
    try:
        resp = requests.get(
            _factory_url(subscription_id, resource_group, factory_name,
                         f"/pipelineruns/{run_id}"),
            headers=auth_headers(),
            params={"api-version": DF_API_VERSION},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return _err(_describe(resp))
        return _ok([resp.json()])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def query_activity_runs(subscription_id: str, resource_group: str, factory_name: str,
                        run_id: str, last_updated_after: str, last_updated_before: str):
    payload = {
        "lastUpdatedAfter": last_updated_after,
        "lastUpdatedBefore": last_updated_before,
    }
    return _post_paged(
        _factory_url(subscription_id, resource_group, factory_name,
                     f"/pipelineruns/{run_id}/queryActivityruns"),
        payload,
    )
