"""Evidence-driven, access-filtered architecture assessments and runtime sensors."""

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from services.access import visible_records
from services.redaction import redact_text


@dataclass(frozen=True)
class Rule:
    rule_id: str
    title: str
    kind: str
    category: str
    fields: tuple[str, ...]
    predicate: object
    recommendation: str
    severity: str = "Medium"


RULES = (
    Rule("tenant-public", "Publish to Web exposure", "Tenant", "Governance", ("publish_to_web_unrestricted",), lambda evidence: evidence["publish_to_web_unrestricted"], "Restrict Publish to Web to approved security groups.", "High"),
    Rule("tenant-trials", "Unrestricted trial capacity creation", "Tenant", "Governance", ("trials_unrestricted",), lambda evidence: evidence["trials_unrestricted"], "Restrict trial creation through tenant policy."),
    Rule("tenant-labels", "Information protection policy gap", "Tenant", "Governance", ("purview_enforced",), lambda evidence: not evidence["purview_enforced"], "Validate Purview policy assignments and label inheritance.", "High"),
    Rule("tenant-orphan", "Orphaned capacity licenses", "Tenant", "Governance", ("orphaned_capacities",), lambda evidence: evidence["orphaned_capacities"] > 0, "Review unassigned capacity licenses against billing ownership."),
    Rule("capacity-size", "Capacity sizing risk", "Capacity", "Capacity", ("history_days", "peak_cu", "allocated_cu"), lambda evidence: evidence["history_days"] >= 14 and evidence["peak_cu"] > evidence["allocated_cu"], "Review 14-30 day peak, memory, concurrency and smoothed demand before scaling.", "High"),
    Rule("capacity-smoothing", "Smoothed capacity pressure", "Capacity", "Capacity", ("smoothed_utilization",), lambda evidence: evidence["smoothed_utilization"] >= 0.9, "Defer low-priority background workloads and review capacity headroom.", "High"),
    Rule("capacity-neighbor", "Noisy neighbor contention", "Capacity", "Capacity", ("spark_reporting_overlap",), lambda evidence: evidence["spark_reporting_overlap"], "Isolate executive reporting from heavy Spark batch windows."),
    Rule("workspace-env", "Environment boundaries missing", "Workspace", "Architecture", ("git_connected", "deployment_stages"), lambda evidence: not evidence["git_connected"] or not {"Dev", "Test", "Prod"}.issubset(evidence["deployment_stages"]), "Connect Git and establish Dev, Test and Prod deployment stages."),
    Rule("workspace-sharing", "Direct item sharing", "Workspace", "Governance", ("direct_shares",), lambda evidence: evidence["direct_shares"] > 0, "Distribute access through Entra security groups and Fabric Apps.", "High"),
    Rule("workspace-medallion", "Medallion separation gap", "Workspace", "Architecture", ("layers",), lambda evidence: not {"Bronze", "Silver", "Gold"}.issubset(evidence["layers"]), "Establish raw, conformed and presentation data boundaries."),
    Rule("workspace-dormant", "Dormant workspace", "Workspace", "Governance", ("days_since_activity", "active_consumers"), lambda evidence: evidence["days_since_activity"] >= 30 and evidence["active_consumers"] == 0, "Confirm owners and retention before archiving unused workloads."),
    Rule("workspace-capacity", "Workspace without assigned capacity", "Workspace", "Governance", ("capacity_assigned",), lambda evidence: not evidence["capacity_assigned"], "Confirm capacity assignment and workspace ownership."),
    Rule("lake-small", "Small Delta files", "Lakehouse", "Architecture", ("files_below_100mb",), lambda evidence: evidence["files_below_100mb"] >= 1000, "Schedule OPTIMIZE bin-compaction after reviewing partition sizes.", "High"),
    Rule("lake-vorder", "V-Order write policy review", "Lakehouse", "Architecture", ("vorder_required", "vorder_enabled"), lambda evidence: evidence["vorder_required"] and not evidence["vorder_enabled"], "Benchmark V-Order for read-heavy consumers before enabling it on write paths."),
    Rule("lake-maintenance", "Delta maintenance overdue", "Lakehouse", "Architecture", ("days_since_optimize", "days_since_vacuum"), lambda evidence: evidence["days_since_optimize"] > 7 or evidence["days_since_vacuum"] > 30, "Schedule OPTIMIZE and retention-safe VACUUM; preserve time travel requirements."),
    Rule("model-fallback", "Direct Lake fallback observed", "SemanticModel", "Direct Lake", ("directlake", "directquery_events"), lambda evidence: evidence["directlake"] and evidence["directquery_events"] > 0, "Inspect fallback cause, guardrails, model memory and unsupported schema before remediation.", "High"),
    Rule("model-memory", "Semantic model memory pressure", "SemanticModel", "Direct Lake", ("memory_gb", "memory_limit_gb"), lambda evidence: evidence["memory_gb"] > evidence["memory_limit_gb"], "Review high-cardinality columns and memory limits for the assigned SKU.", "High"),
    Rule("model-bpa", "BPA model hygiene findings", "SemanticModel", "Architecture", ("bpa_violations",), lambda evidence: len(evidence["bpa_violations"]) > 0, "Review BPA findings for unused columns, bidirectional filters and DAX iterators."),
    Rule("model-consolidation", "Overlapping Import models", "SemanticModel", "Architecture", ("overlapping_import_models",), lambda evidence: evidence["overlapping_import_models"] > 1, "Assess a governed golden semantic model and Direct Lake eligibility."),
    Rule("pipeline-loop", "Row-by-row pipeline loop", "DataPipeline", "Architecture", ("row_by_row_foreach",), lambda evidence: evidence["row_by_row_foreach"], "Replace per-row orchestration with set-based processing."),
    Rule("pipeline-copy", "Shortcut-eligible physical copy", "DataPipeline", "Architecture", ("physical_copy", "shortcut_eligible"), lambda evidence: evidence["physical_copy"] and evidence["shortcut_eligible"], "Validate security, consistency and source support before using a OneLake Shortcut."),
    Rule("pipeline-failure", "Missing failure branch", "DataPipeline", "Architecture", ("has_failure_branch",), lambda evidence: not evidence["has_failure_branch"], "Add On Failure handling and an owner notification branch.", "High"),
    Rule("spark-skew", "Spark partition skew", "Notebook", "Architecture", ("partition_skew_ratio",), lambda evidence: evidence["partition_skew_ratio"] > 3, "Inspect Spark stage telemetry; evaluate AQE and partition strategy."),
    Rule("spark-cache", "Repeated uncached Spark scan", "Notebook", "Architecture", ("repeated_scans", "cache_used"), lambda evidence: evidence["repeated_scans"] > 2 and not evidence["cache_used"], "Benchmark selective caching and unpersist datasets after reuse."),
    Rule("spark-shuffle", "Excessive shuffle spill", "Notebook", "Architecture", ("shuffle_spill_gb",), lambda evidence: evidence["shuffle_spill_gb"] > 10, "Review shuffle partition counts and executor memory from Spark telemetry."),
    Rule("sql-stats", "Business-hours statistics spike", "Warehouse", "Architecture", ("stats_recalculation", "business_hours", "duration_ratio"), lambda evidence: evidence["stats_recalculation"] and evidence["business_hours"] and evidence["duration_ratio"] > 2, "Correlate statistics recalculation with query regressions before changing maintenance windows."),
)


def _valid(value):
    if value is None:
        return False
    if isinstance(value, float):
        return isfinite(value)
    return True


def assessments(principal, records):
    results = []
    for record in visible_records(principal, records):
        evidence = record.get("evidence", {})
        for rule in RULES:
            if record.get("kind") != rule.kind:
                continue
            status = "Not assessed"
            if all(field in evidence and _valid(evidence[field]) for field in rule.fields):
                try:
                    status = "Finding" if rule.predicate(evidence) else "Pass"
                except (TypeError, ValueError, KeyError, ZeroDivisionError):
                    pass
            if rule.rule_id == "capacity-size" and evidence.get("history_days", 0) < 14:
                status = "Not assessed"
            results.append({
                "id": f"{record['id']}:{rule.rule_id}", "record_id": record["id"],
                "tenant_id": record["tenant_id"], "workspace_id": record.get("workspace_id", ""),
                "capacity_id": record.get("capacity_id", ""), "level": record.get("level", "item"),
                "asset": record.get("name", record["id"]), "title": rule.title,
                "category": rule.category, "severity": rule.severity, "status": status,
                "recommendation": rule.recommendation, "rule_id": rule.rule_id,
                "evidence": {field: evidence.get(field) for field in rule.fields},
            })
    return results


def scorecard(results):
    assessed = [result for result in results if result["status"] != "Not assessed"]
    failures = sum(result["status"] == "Finding" for result in assessed)
    score = round(100 * (len(assessed) - failures) / len(assessed)) if assessed else None
    return {"score": score, "grade": "Not assessed" if score is None else "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D",
            "assessed": len(assessed), "total": len(results), "findings": failures}


def incidents(principal, records):
    output = []
    for record in visible_records(principal, records):
        evidence = record.get("evidence", {})
        error = str(evidence.get("error", ""))
        triggers = []
        if (evidence.get("smoothed_utilization", 0) >= 0.9 and evidence.get("sustained_seconds", 0) >= 300) or evidence.get("http_status") == 430:
            triggers.append(("capacity-shield", "Capacity throttling risk", "High"))
        if "java.lang.OutOfMemoryError" in error or "driver memory overflow" in error.lower():
            triggers.append(("spark-oom", "Spark memory exhaustion", "High"))
        if "ConcurrentAppendException" in error:
            triggers.append(("delta-lock", "Concurrent Delta write conflict", "Medium"))
        if evidence.get("directlake") and evidence.get("directquery_events", 0) > 0:
            triggers.append(("directlake-recovery", "Direct Lake fallback", "High"))
        baseline = evidence.get("baseline_seconds", 0)
        elapsed = evidence.get("elapsed_seconds", 0)
        valid_baseline = baseline > 0 and evidence.get("baseline_days", 0) >= 30
        if valid_baseline and elapsed > baseline * 3 and evidence.get("status") == "InProgress":
            triggers.append(("zombie-reaper", "Run exceeds 300% of baseline", "High"))
        if valid_baseline and elapsed > baseline * 1.4 and evidence.get("status") == "InProgress":
            triggers.append(("freshness", "Freshness SLA at risk", "Medium"))
        if evidence.get("status") == "Failed" and not triggers:
            triggers.append(("investigate", "Runtime execution failed", "High"))
        for playbook, title, severity in triggers:
            incident_id = sha256(f"{record['tenant_id']}:{record['id']}:{evidence.get('run_id', '')}:{playbook}".encode()).hexdigest()[:20]
            output.append({"id": incident_id, "record_id": record["id"], "title": title, "severity": severity,
                           "playbook": playbook, "asset": record.get("name", record["id"]),
                           "tenant_id": record["tenant_id"], "workspace_id": record.get("workspace_id", ""),
                           "capacity_id": record.get("capacity_id", ""), "level": record.get("level", "item"),
                           "logs": redact_text(error), "elapsed_seconds": elapsed, "baseline_seconds": baseline,
                           "source": record.get("source", "unknown"), "observed_at": record.get("observed_at", "")})
    return output