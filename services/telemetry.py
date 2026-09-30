"""Read normalized Eventstream/KQL evidence using tenant-bound query parameters."""

import json
from urllib.parse import urlsplit

import requests

from services.access import AccessDenied, visible_records
from services.redaction import redact_text


QUERY = """
declare query_parameters(tenant_id:string, workspace_ids:dynamic, capacity_ids:dynamic, tenant_admin:bool);
FabricPilotTelemetry
| where TenantId == tenant_id
| where WorkspaceId in (workspace_ids)
    or (Level == 'tenant' and tenant_admin)
    or (Level == 'capacity' and (tenant_admin or CapacityId in (capacity_ids)))
| where ObservedAt > ago(30d)
| summarize arg_max(ObservedAt, *) by TenantId, RecordId
| project RecordId, Name, Kind, Level, WorkspaceId, CapacityId, Evidence, ObservedAt
| take 1000
"""


def collect_kql(principal, credential, cluster, database, allowed_tenants):
    if principal.demo or principal.tenant_id not in allowed_tenants:
        raise AccessDenied("The tenant is not approved for this telemetry connection.")
    parsed = urlsplit(cluster)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or parsed.port not in (None, 443)
            or not hostname.endswith((".kusto.windows.net", ".kusto.fabric.microsoft.com"))):
        raise ValueError("Configure an HTTPS Azure Data Explorer or Fabric KQL cluster origin.")
    if not database:
        raise ValueError("A telemetry database is required.")
    origin = f"https://{hostname}"
    token = credential.get_token(origin + "/.default").token
    properties = {"Parameters": {"tenant_id": principal.tenant_id,
                                 "workspace_ids": json.dumps([grant.workspace_id for grant in principal.workspaces]),
                                 "capacity_ids": json.dumps(sorted(principal.capacity_ids)),
                                 "tenant_admin": principal.tenant_admin}}
    response = requests.post(origin + "/v1/rest/query", headers={"Authorization": f"Bearer {token}"},
                             json={"db": database, "csl": QUERY, "properties": json.dumps(properties)},
                             timeout=30, allow_redirects=False)
    response.raise_for_status()
    tables = response.json().get("Tables", [])
    if not tables:
        return []
    table = tables[0]
    columns = [column["ColumnName"] for column in table["Columns"]]
    records = []
    for row in table["Rows"]:
        values = dict(zip(columns, row))
        evidence = values["Evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        if not isinstance(evidence, dict):
            raise ValueError("Telemetry Evidence must be a JSON object.")
        if "error" in evidence:
            evidence["error"] = redact_text(str(evidence["error"]))
        records.append({"id": values["RecordId"], "name": values["Name"], "kind": values["Kind"],
                        "level": values["Level"], "workspace_id": values["WorkspaceId"],
                        "capacity_id": values["CapacityId"], "tenant_id": principal.tenant_id,
                        "evidence": evidence, "observed_at": values["ObservedAt"], "source": "kql"})
    return visible_records(principal, records)