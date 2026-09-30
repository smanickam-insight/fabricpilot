"""A synthetic tenant, never used as live authorization or execution evidence."""

from datetime import datetime, timedelta, timezone

from services.access import Principal, WorkspaceGrant


def demo_principal():
    return Principal("demo-tenant", "demo-operator", "Demo operator",
                     (WorkspaceGrant("demo-finance", "Finance Production", "Admin", "demo-f64"),
                      WorkspaceGrant("demo-engineering", "Data Engineering", "Contributor", "demo-f64")),
                     capacity_ids=frozenset({"demo-f64"}), tenant_admin=True, demo=True,
                     expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


def demo_records():
    seeds = [
        ("tenant", "Contoso sample estate", "Tenant", "tenant", "", {"publish_to_web_unrestricted": True, "trials_unrestricted": False, "purview_enforced": True, "orphaned_capacities": 1}),
        ("capacity", "Analytics Production / F64", "Capacity", "capacity", "", {"history_days": 30, "peak_cu": 78, "allocated_cu": 64, "smoothed_utilization": 0.93, "sustained_seconds": 420, "spark_reporting_overlap": True, "http_status": 430, "cu_history": [32, 35, 43, 51, 47, 63, 78, 71, 58, 49, 42, 38]}),
        ("finance", "Finance Production", "Workspace", "workspace", "demo-finance", {"git_connected": True, "deployment_stages": ["Dev", "Test", "Prod"], "direct_shares": 3, "layers": ["Bronze", "Silver", "Gold"], "days_since_activity": 0, "active_consumers": 120, "capacity_assigned": True}),
        ("engineering", "Data Engineering", "Workspace", "workspace", "demo-engineering", {"git_connected": False, "deployment_stages": ["Dev"], "direct_shares": 0, "layers": ["Bronze", "Silver"], "days_since_activity": 45, "active_consumers": 0, "capacity_assigned": True}),
        ("sales-delta", "Gold sales lakehouse", "Lakehouse", "item", "demo-finance", {"files_below_100mb": 4280, "vorder_required": True, "vorder_enabled": False, "days_since_optimize": 12, "days_since_vacuum": 18, "error": "ConcurrentAppendException: overlapping OPTIMIZE and append on sales_delta", "status": "Failed", "run_id": "demo-lock"}),
        ("sales-model", "Executive sales model", "SemanticModel", "item", "demo-finance", {"directlake": True, "directquery_events": 18, "memory_gb": 28, "memory_limit_gb": 25, "bpa_violations": ["Unused high-cardinality column", "Bidirectional cross-filter"], "overlapping_import_models": 3, "error": "DirectQuery event observed on Direct Lake model; memory guardrail exceeded"}),
        ("ingest", "Daily sales ingestion", "DataPipeline", "item", "demo-finance", {"row_by_row_foreach": True, "physical_copy": True, "shortcut_eligible": True, "has_failure_branch": False, "baseline_days": 30, "baseline_seconds": 1200, "elapsed_seconds": 1980, "status": "InProgress", "run_id": "demo-ingest", "duration_history": [1100, 1190, 1210, 1180, 1220, 1190, 1980]}),
        ("spark", "Customer enrichment", "Notebook", "item", "demo-engineering", {"partition_skew_ratio": 7.4, "repeated_scans": 4, "cache_used": False, "shuffle_spill_gb": 28, "error": "java.lang.OutOfMemoryError: Java heap space in customer enrichment stage", "status": "Failed", "run_id": "demo-oom"}),
        ("stalled", "Legacy full-load job", "DataPipeline", "item", "demo-engineering", {"row_by_row_foreach": False, "physical_copy": True, "shortcut_eligible": False, "has_failure_branch": True, "baseline_days": 30, "baseline_seconds": 600, "elapsed_seconds": 2160, "status": "InProgress", "run_id": "demo-stalled", "duration_history": [580, 605, 590, 615, 600, 610, 2160]}),
        ("warehouse", "Finance warehouse", "Warehouse", "item", "demo-finance", {"stats_recalculation": True, "business_hours": True, "duration_ratio": 3.2}),
    ]
    return [{"id": record_id, "name": name, "kind": kind, "level": level,
             "tenant_id": "demo-tenant", "workspace_id": workspace, "capacity_id": "demo-f64",
             "evidence": evidence, "source": "demo", "observed_at": "2026-10-01T14:30:00Z"}
            for record_id, name, kind, level, workspace, evidence in seeds]