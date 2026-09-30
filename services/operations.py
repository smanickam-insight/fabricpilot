"""Durable tenant-scoped action approvals and guarded Fabric job execution."""

from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

import requests

from services.access import AccessDenied, Action, Resource, authorize, visible_records


PLAYBOOKS = {
    "capacity-shield": {"name": "Capacity Throttling Shield", "trigger": "Smoothed CU >= 90% for 5 minutes, or HTTP 430", "action": "scale", "steps": ["Queue low-priority background work.", "Validate production impact and approved F-SKU budget.", "Scale only with Fabric capacity and Azure write permissions."], "adapter": False},
    "spark-oom": {"name": "Spark OOM Auto-Tuning", "trigger": "OutOfMemoryError or driver memory overflow", "action": "run", "steps": ["Inspect redacted driver logs and skew.", "Review bounded executor-memory and AQE changes.", "Run a tested notebook revision and validate output."], "adapter": False},
    "delta-lock": {"name": "Delta File Lock Resolution", "trigger": "ConcurrentAppendException", "action": "run", "steps": ["Verify the original job failed and writes are idempotent.", "Schedule retries after 30, 60 and 120 seconds.", "Stop after three attempts and escalate to the table owner."], "adapter": True},
    "directlake-recovery": {"name": "Direct Lake Fallback Recovery", "trigger": "DirectQuery events on a Direct Lake model", "action": "run", "steps": ["Distinguish memory, schema and fragmentation causes.", "Run approved compaction only when fragmentation is causal.", "Reload and validate the semantic model using a configured adapter."], "adapter": False},
    "zombie-reaper": {"name": "Zombie Job Reaper", "trigger": "Active run exceeds 300% of a 30-day baseline", "action": "cancel", "steps": ["Confirm the job is still running and has no protected dependencies.", "Request cancellation of the verified job instance.", "Confirm cancellation and notify downstream owners."], "adapter": True},
    "freshness": {"name": "Data Freshness & SLA Prediction", "trigger": "Active ingestion > 140% of a 30-day baseline", "action": "run", "steps": ["Compare elapsed duration with the historical baseline.", "Estimate downstream impact using dependency and throughput telemetry.", "Notify report owners through an approved Teams or Slack connector."], "adapter": False},
}


def utcnow():
    return datetime.now(timezone.utc)


class OperationStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    tenant TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (tenant, id));
                CREATE TABLE IF NOT EXISTS plans (
                    tenant TEXT NOT NULL, id TEXT NOT NULL, actor TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL,
                    PRIMARY KEY (tenant, id));
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
                    actor TEXT NOT NULL, plan_id TEXT NOT NULL, event TEXT NOT NULL,
                    created TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put_records(self, principal, records):
        visible = visible_records(principal, records)
        if len(visible) != len(records):
            raise AccessDenied("Snapshot contains records outside the authorized scope.")
        with self.connect() as connection:
            for record in records:
                connection.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)",
                                   (principal.tenant_id, record["id"], json.dumps(record)))

    def records(self, principal):
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM records WHERE tenant = ?", (principal.tenant_id,)).fetchall()
        return visible_records(principal, [json.loads(row["payload"]) for row in rows])

    def create_plan(self, principal, incident, record, attempt=0):
        playbook = PLAYBOOKS[incident["playbook"]]
        resource = Resource(record["tenant_id"], record.get("workspace_id", ""),
                            record.get("capacity_id", ""), record.get("id", ""))
        authorize(principal, Action(playbook["action"]), resource)
        if incident["record_id"] != record["id"] or incident["tenant_id"] != record["tenant_id"]:
            raise AccessDenied("Incident and resource do not match.")
        if attempt not in (0, 1, 2):
            raise ValueError("Maximum retry count reached.")
        plan_id = str(uuid4())
        delay = (30, 60, 120)[attempt] if incident["playbook"] == "delta-lock" else 0
        payload = {"resource": asdict(resource), "playbook": incident["playbook"], "incident_id": incident["id"],
                   "record": record, "attempt": attempt, "demo": principal.demo or record.get("source") == "demo",
                   "due_at": (utcnow() + timedelta(seconds=delay)).isoformat(),
                   "expires_at": (utcnow() + timedelta(minutes=10)).isoformat()}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT id, payload, created FROM plans WHERE tenant = ? AND actor = ?", (principal.tenant_id, principal.object_id)).fetchall()
            for row in existing:
                previous = json.loads(row["payload"])
                if previous["incident_id"] == incident["id"] and previous["attempt"] == attempt:
                    return row["id"]
            connection.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)",
                               (principal.tenant_id, plan_id, principal.object_id, "Pending approval", json.dumps(payload), utcnow().isoformat()))
            self._audit(connection, principal, plan_id, "Plan created")
        return plan_id

    def _audit(self, connection, principal, plan_id, event):
        connection.execute("INSERT INTO audit (tenant, actor, plan_id, event, created) VALUES (?, ?, ?, ?, ?)",
                           (principal.tenant_id, principal.object_id, plan_id, event, utcnow().isoformat()))

    def get_plan(self, principal, plan_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE tenant = ? AND actor = ? AND id = ?",
                                     (principal.tenant_id, principal.object_id, plan_id)).fetchone()
        if row is None:
            raise AccessDenied("Action plan not found in this identity's scope.")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        authorize(principal, Action(PLAYBOOKS[result["payload"]["playbook"]]["action"]), Resource(**result["payload"]["resource"]))
        return result

    def transition(self, principal, plan_id, expected, status):
        self.get_plan(principal, plan_id)
        with self.connect() as connection:
            cursor = connection.execute("UPDATE plans SET status = ? WHERE tenant = ? AND actor = ? AND id = ? AND status = ?",
                                        (status, principal.tenant_id, principal.object_id, plan_id, expected))
            if cursor.rowcount != 1:
                raise ValueError("Action state changed or has already been processed.")
            self._audit(connection, principal, plan_id, status)

    def approve(self, principal, plan_id):
        self.transition(principal, plan_id, "Pending approval", "Approved")

    def audit(self, principal):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT plan_id, event, created FROM audit WHERE tenant = ? AND actor = ? ORDER BY sequence DESC LIMIT 100",
                (principal.tenant_id, principal.object_id))]


def execute_plan(store, principal, plan_id, credential, enabled=False):
    plan = store.get_plan(principal, plan_id)
    payload = plan["payload"]
    if not enabled or payload["demo"] or principal.demo:
        raise AccessDenied("Live execution is disabled; demo data can never execute against Fabric.")
    if plan["status"] != "Approved":
        raise AccessDenied("Explicit approval is required.")
    if utcnow() >= datetime.fromisoformat(payload["expires_at"]):
        raise AccessDenied("Action plan expired. Reassess before executing.")
    if utcnow() < datetime.fromisoformat(payload["due_at"]):
        raise AccessDenied("Retry backoff has not elapsed.")
    playbook = PLAYBOOKS[payload["playbook"]]
    if not playbook["adapter"]:
        raise AccessDenied("This playbook requires a tenant-specific execution adapter.")
    record = payload["record"]
    if record.get("source") != "fabric-rest":
        raise AccessDenied("Live execution requires server-collected Fabric evidence.")
    workspace = str(UUID(record["workspace_id"]))
    item = str(UUID(record["id"]))
    job = str(UUID(record["evidence"]["run_id"]))
    origin = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace}/items/{item}"
    headers = {"Authorization": f"Bearer {credential.get_token('https://api.fabric.microsoft.com/.default').token}"}
    response = requests.get(f"{origin}/jobs/instances/{job}", headers=headers, timeout=20, allow_redirects=False)
    response.raise_for_status()
    status = response.json().get("status")
    if payload["playbook"] == "delta-lock":
        if status != "Failed" or not record["evidence"].get("idempotent_retry_approved"):
            raise AccessDenied("Retry requires a failed job and explicit idempotent-write approval.")
        job_type = record["evidence"].get("job_type")
        if job_type not in {"Pipeline", "RunNotebook"}:
            raise AccessDenied("Unsupported job type.")
        url = f"{origin}/jobs/{job_type}/instances"
    else:
        if status != "InProgress" or not record["evidence"].get("cancellation_approved"):
            raise AccessDenied("Cancellation requires an active job and dependency-owner approval.")
        url = f"{origin}/jobs/instances/{job}/cancel"
    store.transition(principal, plan_id, "Approved", "Dispatching")
    try:
        response = requests.post(url, headers=headers, timeout=30, allow_redirects=False)
        if response.status_code != 202:
            store.transition(principal, plan_id, "Dispatching", "Rejected")
            raise RuntimeError(f"Fabric rejected the request ({response.status_code}).")
    except requests.RequestException as exc:
        store.transition(principal, plan_id, "Dispatching", "Outcome unknown; reconcile before retry")
        raise RuntimeError("Execution outcome is unknown; do not retry automatically.") from exc
    store.transition(principal, plan_id, "Dispatching", "Accepted; awaiting verification")
    return {"status": "Accepted; awaiting verification", "request_id": response.headers.get("request-id", "")}