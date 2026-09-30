"""
Remediation / Resolution Plan for RECURRING failures.

The Dashboard already flags *how many* errors are recurring. This module turns
that signal into an actionable **resolution plan**: for every error signature
that keeps coming back (the same Pipeline | Activity | ErrorCategory failing on
more than one day, or more than once), it produces:

  * a severity/priority score (frequency x persistence x trend),
  * the likely root cause,
  * an **LLM-generated**, error-grounded, multi-step remediation playbook
    (the static PLAYBOOKS below are now only the offline fallback),
  * a suggested owner/team,
  * first-seen / last-seen / occurrences / days-affected,
  * a target SLA date.

It operates on the enriched detail DataFrame produced by services.analytics
(`Analysis.detail_df`), so it needs no live Azure connection and works for
Demo Data, Live Log Analytics results, AND Fabric job runs.

Nothing here mutates Azure or Fabric — it only reads the failure frame and
emits a plan that a human (or a downstream Logic App / Planner task) can act on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from services.llm_remediation import generate_playbooks_bulk, llm_enabled

# --------------------------------------------------------------------------- #
# FALLBACK remediation playbooks
#
# These are NO LONGER the primary source of remediation steps. When an LLM
# provider is configured (services.llm_remediation), steps are generated from
# the *actual* error text of each recurring signature. These static playbooks
# are used only when the LLM is unavailable, times out, is over quota, or
# returns output that fails validation — so the plan is always complete.
#
# Category-specific remediation playbooks
# Keys match services.analytics.ERROR_RULES labels (plus the two fallbacks).
# Each playbook: (owner_team, sla_days, [ordered remediation steps]).
# --------------------------------------------------------------------------- #
PLAYBOOKS: dict[str, tuple[str, int, list[str]]] = {
    "Timeout": (
        "Data Engineering", 3,
        [
            "Confirm the activity's `timeout` setting and raise it if the source is legitimately slow.",
            "Check source responsiveness (DB load, API latency) during the failure window.",
            "Right-size the Integration Runtime (more cores / bigger cluster) or reduce batch/partition size.",
            "Add a retry policy with exponential backoff so transient slowness self-heals.",
            "If a Copy activity, tune parallelCopies / DIUs and enable staging for large volumes.",
        ],
    ),
    "SQL execution error": (
        "Data Engineering", 2,
        [
            "Open the failing query / stored procedure and validate syntax and object names.",
            "Confirm the schema hasn't drifted (renamed/dropped columns, type changes).",
            "Verify the linked-service principal has the required table/proc permissions.",
            "Add defensive checks (NULL handling, explicit column lists) to harden the query.",
            "Re-run against a known-good sample to confirm the fix before closing.",
        ],
    ),
    "Databricks Error": (
        "Data Engineering / Platform", 3,
        [
            "Open the Databricks run page / Spark UI from the activity output link.",
            "Check cluster health: driver OOM, autoscaling limits, spot-instance eviction.",
            "Review the notebook's last successful commit vs current for breaking changes.",
            "Pin a stable cluster/runtime version and set adequate driver/worker sizes.",
            "Add notebook-level exception handling and idempotent writes for safe retries.",
        ],
    ),
    "Spark / Fabric capacity": (
        "Fabric Platform / Capacity Admin", 2,
        [
            "Open the Fabric Capacity Metrics app and check CU utilisation and throttling during the window.",
            "Identify the top CU-consuming items (notebooks, pipelines, semantic models) in the workspace.",
            "Check the Spark pool / starter pool sizing and concurrency limits on the workspace.",
            "Stagger schedules so heavy notebooks and refreshes do not overlap on the same capacity.",
            "Scale the capacity SKU (or enable autoscale) if throttling is sustained rather than spiky.",
        ],
    ),
    "Connection Failed": (
        "Platform / Networking", 2,
        [
            "Verify the linked service credentials and that the endpoint is reachable.",
            "Check firewall / NSG / private-endpoint rules and the IR's network line of sight.",
            "Confirm DNS resolves and any VPN / ExpressRoute path is healthy.",
            "Test the connection from the same Integration Runtime used by the pipeline.",
            "Add retry-with-backoff to absorb brief network blips.",
        ],
    ),
    "Service throttling (429)": (
        "Data Engineering", 2,
        [
            "Add a retry policy with exponential backoff + jitter on the throttled activity.",
            "Stagger trigger schedules so concurrent pipelines don't hammer the same sink.",
            "Reduce parallelism (parallelCopies / concurrent activities) to stay under limits.",
            "Request a higher service tier / quota if throttling is sustained.",
            "Consider batching or a queue to smooth spiky request rates.",
        ],
    ),
    "Auth / RBAC failure": (
        "Security / IAM", 1,
        [
            "Confirm the managed identity / service principal still exists and isn't expired.",
            "Re-check role assignments (Reader/Contributor/Key Vault access) on the target scope.",
            "Rotate and update any expired secret/certificate in Key Vault.",
            "Validate Conditional Access / tenant policies aren't blocking the identity.",
            "Re-run after granting least-privilege access and confirm success.",
        ],
    ),
    "Source data error": (
        "Data Engineering / Source Owner", 3,
        [
            "Confirm the expected source file / dataset actually arrived (path, timestamp).",
            "Validate the schema against the contract (columns, order, encoding, delimiter).",
            "Coordinate with the upstream/source owner on any schema-drift changes.",
            "Add schema validation + quarantine for bad rows so one file can't fail the run.",
            "Backfill or reprocess once the corrected source is available.",
        ],
    ),
    "Other error": (
        "Data Engineering", 3,
        [
            "Open the full activity output/error payload for the exact failure detail.",
            "Reproduce with a minimal run to isolate the failing step.",
            "Check recent pipeline/linked-service changes that correlate with onset.",
            "Add targeted logging around the failing activity.",
            "Document the root cause once identified and add a guard to prevent recurrence.",
        ],
    ),
    "Unknown error": (
        "Data Engineering", 3,
        [
            "Enable raw (unredacted) error text on the Dashboard to read the full message.",
            "Cross-reference the run in ADF Monitoring / Fabric Monitor / Log Analytics for context.",
            "Reproduce and bisect recent changes to locate the trigger.",
            "Add diagnostics/logging to capture more detail on the next occurrence.",
            "Once understood, move it into a specific category playbook.",
        ],
    ),
}

_DEFAULT_PLAYBOOK = PLAYBOOKS["Other error"]


@dataclass
class RemediationItem:
    pipeline: str
    activity: str
    error_category: str
    occurrences: int
    days_affected: int
    first_seen: dt.date
    last_seen: dt.date
    still_active_today: bool
    severity: str
    priority_score: int
    root_cause: str
    owner: str
    target_date: dt.date
    steps: list[str] = field(default_factory=list)

    # ---- LLM enrichment ---------------------------------------------------
    # Populated when the playbook came from the LLM; empty on static fallback.
    preventive: list[str] = field(default_factory=list)
    validation: str = ""
    confidence: str = ""                 # High | Medium | Low
    plan_source: str = "fallback"        # "llm" | "fallback"
    activity_type: str = ""
    is_notebook: bool = False
    sample_errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def _severity(occurrences: int, days_affected: int, still_active: bool) -> tuple[str, int]:
    """
    Simple, explainable scoring:
      score = occurrences + 5*days_affected (+3 if still failing today)
    """
    score = occurrences + 5 * days_affected + (3 if still_active else 0)
    if score >= 25:
        return "🔴 Critical", score
    if score >= 15:
        return "🟠 High", score
    if score >= 8:
        return "🟡 Medium", score
    return "🟢 Low", score


# Columns that may carry the raw error text, in order of preference.
# `Error` is what services.analytics.enrich() produces for both ADF (Log
# Analytics ADFActivityRun) and Fabric (fabric_api.jobs_to_analytics_frame).
_ERROR_COLS = ["Error", "ErrorMessage", "ErrorText", "Message",
               "FailureMessage", "ErrorDetail", "RawError"]


def _sample_errors(g: pd.DataFrame, limit: int = 3) -> list[str]:
    """Up to `limit` distinct raw error strings for this signature."""
    for col in _ERROR_COLS:
        if col in g.columns:
            vals = g[col].dropna().astype(str).str.strip()
            vals = vals[vals != ""].drop_duplicates().head(limit).tolist()
            if vals:
                return vals
    return []


def build_remediation_plan(
    detail_df: pd.DataFrame,
    min_days: int = 2,
    use_llm: bool | None = None,
    platform: str = "Azure Data Factory",
) -> list[RemediationItem]:
    """
    Build a resolution plan for recurring failures.

    A signature is "recurring" when it failed on >= `min_days` distinct days
    (i.e. it persisted rather than being a one-off). Ordered by priority.

    Parameters
    ----------
    use_llm : bool | None
        True  -> always attempt the LLM (each item falls back independently)
        False -> static PLAYBOOKS only (offline / zero-cost mode)
        None  -> auto: use the LLM when a provider is configured
    platform : str
        Passed to the prompt so the model names the right UI surface,
        e.g. "Azure Data Factory" or "Microsoft Fabric".
    """
    if detail_df is None or detail_df.empty:
        return []

    df = detail_df.copy()
    if "ErrorSignature" not in df.columns:
        df["ErrorSignature"] = (
            df["PipelineName"] + " | " + df["ActivityName"] + " | " + df["ErrorCategory"]
        )

    today = df["Date"].max()
    grouped = df.groupby("ErrorSignature")
    items: list[RemediationItem] = []

    for sig, g in grouped:
        days_affected = g["Date"].nunique()
        if days_affected < min_days:
            continue  # not recurring enough to warrant a plan

        occurrences = int(len(g))
        first_seen = g["Date"].min()
        last_seen = g["Date"].max()
        still_active = bool(last_seen == today)

        pipeline = g["PipelineName"].iloc[0]
        activity = g["ActivityName"].iloc[0]
        category = g["ErrorCategory"].iloc[0]
        root_cause = (
            g["RecommendedAction"].iloc[0]
            if "RecommendedAction" in g.columns else "Review activity output"
        )

        owner, sla_days, steps = PLAYBOOKS.get(category, _DEFAULT_PLAYBOOK)
        severity, score = _severity(occurrences, days_affected, still_active)
        target_date = today + dt.timedelta(days=sla_days)

        activity_type = str(g["ActivityType"].iloc[0]) if "ActivityType" in g.columns else ""
        is_notebook = bool(g["IsNotebook"].iloc[0]) if "IsNotebook" in g.columns else False

        items.append(RemediationItem(
            pipeline=pipeline, activity=activity, error_category=category,
            occurrences=occurrences, days_affected=days_affected,
            first_seen=first_seen, last_seen=last_seen, still_active_today=still_active,
            severity=severity, priority_score=score, root_cause=root_cause,
            owner=owner, target_date=target_date, steps=list(steps),
            activity_type=activity_type, is_notebook=is_notebook,
            sample_errors=_sample_errors(g),
        ))

    items.sort(key=lambda i: i.priority_score, reverse=True)

    if use_llm is None:
        use_llm = llm_enabled()
    if use_llm and items:
        _enrich_with_llm(items, today, platform)

    return items


def _enrich_with_llm(items: list[RemediationItem], today, platform: str) -> None:
    """
    Replace each item's steps/owner/SLA/root-cause with an LLM-generated,
    error-grounded playbook.

    All signatures are processed concurrently. Any item whose call fails keeps
    its static playbook untouched, so the plan is never partial. Mutates
    `items` in place.
    """
    requests = [
        dict(
            pipeline=it.pipeline,
            activity=it.activity,
            category=it.error_category,
            error_messages=it.sample_errors,
            occurrences=it.occurrences,
            days_affected=it.days_affected,
            first_seen=it.first_seen,
            last_seen=it.last_seen,
            still_active=it.still_active_today,
            hint=it.root_cause,
            activity_type=it.activity_type,
            is_notebook=it.is_notebook,
            platform=platform,
            fallback_owner=it.owner,
            fallback_sla=max((it.target_date - today).days, 1),
            fallback_steps=it.steps,
        )
        for it in items
    ]

    for it, pb in zip(items, generate_playbooks_bulk(requests)):
        it.steps = pb.steps
        it.owner = pb.owner
        it.preventive = pb.preventive
        it.validation = pb.validation
        it.confidence = pb.confidence
        it.plan_source = pb.source
        it.target_date = today + dt.timedelta(days=pb.sla_days)
        if pb.source == "llm" and pb.root_cause:
            it.root_cause = pb.root_cause


def plan_summary_df(items: list[RemediationItem]) -> pd.DataFrame:
    """One-row-per-recurring-failure summary table for the UI / CSV export."""
    if not items:
        return pd.DataFrame(columns=[
            "Priority", "Pipeline", "Activity", "Root Cause Category", "Occurrences",
            "Days Affected", "Still Failing Today", "Owner", "Target Date",
            "Plan Source", "Confidence",
        ])
    return pd.DataFrame([
        {
            "Priority": it.severity,
            "Pipeline": it.pipeline,
            "Activity": it.activity,
            "Root Cause Category": it.error_category,
            "Occurrences": it.occurrences,
            "Days Affected": it.days_affected,
            "Still Failing Today": "Yes" if it.still_active_today else "No",
            "Owner": it.owner,
            "Target Date": it.target_date.isoformat(),
            "Plan Source": "🤖 AI" if it.plan_source == "llm" else "📘 Static",
            "Confidence": it.confidence or "—",
        }
        for it in items
    ])


def plan_to_markdown(items: list[RemediationItem], generated_for: str = "") -> str:
    """Render the full resolution plan as a shareable Markdown document."""
    now = dt.datetime.now(dt.timezone.utc)
    lines = [
        "# Recurring-Failure Remediation Plan",
        f"_Generated (UTC): {now:%Y-%m-%d %H:%M}_"
        + (f" · Scope: {generated_for}" if generated_for else ""),
        "",
    ]
    if not items:
        lines.append("✅ **No recurring failures** — nothing to remediate in this window. 🎉")
        return "\n".join(lines)

    lines.append(f"**{len(items)} recurring failure(s)** need a resolution plan, "
                 "ordered by priority.\n")

    for idx, it in enumerate(items, 1):
        lines += [
            f"## {idx}. {it.severity} — `{it.pipeline}` / `{it.activity}`",
            f"- **Root cause category:** {it.error_category}",
            f"- **Occurrences:** {it.occurrences} across **{it.days_affected} day(s)** "
            f"({it.first_seen} → {it.last_seen})",
            f"- **Still failing today:** {'Yes' if it.still_active_today else 'No'}",
            f"- **Suggested owner:** {it.owner}",
            f"- **Target resolution date:** {it.target_date.isoformat()}",
            f"- **Quick recommendation:** {it.root_cause}",
            "",
            "**Remediation steps:**",
        ]
        lines += [f"{n}. {step}" for n, step in enumerate(it.steps, 1)]

        if it.validation:
            lines += ["", f"**Validation:** {it.validation}"]
        if it.preventive:
            lines += ["", "**Preventive actions:**"]
            lines += [f"- {p}" for p in it.preventive]

        origin = "AI-generated from the raw error text" if it.plan_source == "llm" \
            else "static fallback playbook"
        conf = f" · confidence: {it.confidence}" if it.confidence else ""
        lines += ["", f"_Plan source: {origin}{conf}_", ""]

    return "\n".join(lines)


def plan_to_csv(items: list[RemediationItem]) -> bytes:
    """CSV export: one row per (recurring failure x step) for ticket import."""
    rows = []
    for it in items:
        for n, step in enumerate(it.steps, 1):
            rows.append({
                "Priority": it.severity,
                "Pipeline": it.pipeline,
                "Activity": it.activity,
                "Category": it.error_category,
                "Occurrences": it.occurrences,
                "DaysAffected": it.days_affected,
                "StillFailingToday": "Yes" if it.still_active_today else "No",
                "Owner": it.owner,
                "TargetDate": it.target_date.isoformat(),
                "StepNo": n,
                "Step": step,
                "PlanSource": it.plan_source,
                "Confidence": it.confidence,
                "Validation": it.validation,
                "Preventive": " | ".join(it.preventive),
            })
    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["Priority", "Pipeline", "Activity", "Category", "Occurrences",
                 "DaysAffected", "StillFailingToday", "Owner", "TargetDate", "StepNo", "Step",
                 "PlanSource", "Confidence", "Validation", "Preventive"]
    )
    return df.to_csv(index=False).encode("utf-8")
