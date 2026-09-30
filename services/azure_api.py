"""
Thin REST client over Azure Resource Manager (subscriptions, Log Analytics
workspace discovery) and the Log Analytics Query API (KQL execution).

Every function returns a dict shaped like:
    {"value": [...], "error": None, "pages_fetched": n, "truncated": bool}
so the UI layer never has to handle exceptions itself.

Read-only by design — this client only issues GET / query-with-body POST
calls. It cannot create, modify, or delete any Azure resource.

Endpoints used:
    GET  /subscriptions                                                           (api-version 2022-12-01)
    GET  /subscriptions/{sub}/providers/Microsoft.OperationalInsights/workspaces  (api-version 2022-10-01)
    POST https://api.loganalytics.io/v1/workspaces/{customerId}/query
"""

from __future__ import annotations

import requests

from services.auth import ARM_BASE, LOG_ANALYTICS_BASE, arm_headers, log_analytics_headers
from services.redaction import redact_text

ARM_API_VERSION = "2022-12-01"
LAW_API_VERSION = "2022-10-01"
MAX_PAGES = 20
REQUEST_TIMEOUT = 60


# ---------------------------------------------------------------- internals
def _ok(value, pages=1, truncated=False, **extra):
    out = {"value": value, "error": None, "pages_fetched": pages, "truncated": truncated}
    out.update(extra)
    return out


def _err(message):
    return {"value": [], "error": redact_text(str(message)), "pages_fetched": 0, "truncated": False}


def _describe(resp: requests.Response) -> str:
    """Summarise a failed response. Body is redacted before it reaches the UI."""
    try:
        body = resp.json()
        err = body.get("error", body)
        return redact_text(f"{err.get('code', resp.status_code)}: "
                           f"{err.get('message', resp.text[:400])}")
    except Exception:  # noqa: BLE001
        return redact_text(f"{resp.status_code}: {resp.text[:400]}")


def _get_paged(url: str, params: dict | None = None):
    """GET an ARM collection, following nextLink."""
    try:
        headers = arm_headers()
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


# ------------------------------------------------------------ public API
def list_subscriptions():
    """All subscriptions the signed-in user can read."""
    result = _get_paged(f"{ARM_BASE}/subscriptions", {"api-version": ARM_API_VERSION})
    if result["error"]:
        return result
    enabled = [s for s in result["value"] if s.get("state") in (None, "Enabled", "Warned")]
    return _ok(enabled, result["pages_fetched"], result["truncated"])


def list_log_analytics_workspaces(subscription_id: str):
    """All Log Analytics workspaces in a subscription (with customerId GUID)."""
    return _get_paged(
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.OperationalInsights/workspaces",
        {"api-version": LAW_API_VERSION},
    )


def run_kql_query(customer_id: str, query: str, timespan: str | None = None):
    """
    Execute a KQL query against a Log Analytics workspace's query API.

    customer_id: the workspace GUID (properties.customerId from the ARM listing,
                 NOT the ARM resource id).
    timespan:    optional ISO-8601 duration/interval, e.g. "P7D" or an interval
                 "2026-09-01T00:00:00Z/2026-09-08T00:00:00Z". If the query
                 already contains its own `ago()` filter this can be omitted.
    """
    try:
        headers = log_analytics_headers()
        payload = {"query": query}
        if timespan:
            payload["timespan"] = timespan

        resp = requests.post(
            f"{LOG_ANALYTICS_BASE}/v1/workspaces/{customer_id}/query",
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return _err(_describe(resp))

        body = resp.json()
        tables = body.get("tables", [])
        if not tables:
            return _ok([])
        table = tables[0]
        columns = [c["name"] for c in table.get("columns", [])]
        rows = table.get("rows", [])
        records = [dict(zip(columns, row)) for row in rows]
        return _ok(records)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
