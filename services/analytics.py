"""
Analysis engine for ADF / Fabric failure data pulled from Log Analytics
(or Demo Data).

Given a DataFrame with columns:
    TimeGenerated, PipelineName, ActivityName, ActivityType, Status, Error, PipelineRunId

this module derives every metric shown on the dashboard: daily failure counts,
resolved/unresolved tracking, new-vs-recurring errors, anomaly detection, and
root-cause categorization.

The same engine is reused by the Fabric Error Analytics tab — Fabric job
failures are normalised into this identical schema by services.fabric_api.
"""

from __future__ import annotations

import re
import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ============================================================================
# ERROR CATEGORIZATION (root-cause mapping)
# ============================================================================
ERROR_RULES = [
    ("Timeout",              r"timeout|timed out",                         "Increase timeout / adjust batch size"),
    ("SQL execution error",  r"sql|syntax error|invalid column|query fail", "Validate query / schema"),
    ("Databricks Error",     r"databricks|cluster|notebook exec",          "Check cluster config / notebook logs"),
    ("Spark / Fabric capacity", r"capacity|throttled capacity|cu limit|spark session|livy",
                                                                           "Review capacity SKU / Spark pool sizing"),
    ("Connection Failed",    r"connection|network|unreachable|refused",     "Verify linked service / firewall rules"),
    ("Service throttling (429)", r"429|throttl|too many requests",          "Add retry policy / backoff"),
    ("Auth / RBAC failure",  r"unauthorized|forbidden|403|rbac|permission", "Verify permissions / managed identity roles"),
    ("Source data error",    r"source|schema drift|missing file|not found", "Validate source data & schema"),
]

NOTEBOOK_ACTIVITY_TYPES = {
    "DatabricksNotebook", "DatabricksSparkPython", "DatabricksSparkJar",
    "TridentNotebook", "FabricNotebook", "SynapseNotebook",
}


def categorize_error(msg: str):
    if not isinstance(msg, str) or not msg:
        return "Unknown error", "Investigate manually"
    low = msg.lower()
    for label, pattern, action in ERROR_RULES:
        if re.search(pattern, low):
            return label, action
    return "Other error", "Review activity output for details"


# ============================================================================
# DEMO DATA GENERATOR (used when no Azure connection is configured)
# ============================================================================
PIPELINES = ["Customer_ETL", "Sales_ETL", "Finance_ETL", "SAP_Refresh", "Master_Data_Load"]

ACTIVITIES = {
    "Customer_ETL":     [("CopyOrders", "Copy"), ("Customer_Load_NB", "DatabricksNotebook")],
    "Sales_ETL":        [("CopyData", "Copy"), ("Sales_Transform_NB", "DatabricksNotebook")],
    "Finance_ETL":      [("Lookup", "Lookup"), ("Ledger_Process_NB", "DatabricksNotebook")],
    "SAP_Refresh":      [("StoredProcedure", "SqlServerStoredProcedure")],
    "Master_Data_Load": [("CopyData", "Copy"), ("NotebookRun", "DatabricksNotebook")],
}

ERROR_MESSAGES = [
    "Operation timed out after 3600 seconds",
    "SQL execution error: invalid column name 'CustID'",
    "Databricks Error: cluster terminated unexpectedly",
    "Connection Failed: source connection refused",
    "Service throttling (429): too many requests to sink",
    "Unauthorized (403): RBAC permission denied on key vault",
    "Source data error: schema drift detected, missing file",
]


def generate_demo_data(lookback_days: int, seed: int = 42) -> pd.DataFrame:
    """Generates a synthetic ADFActivityRun-style failed-activity dataset."""
    rng = np.random.default_rng(seed)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    records = []
    run_id = 10000

    for day_offset in range(lookback_days, -1, -1):
        day = now - dt.timedelta(days=day_offset)
        base = 18 + (lookback_days - day_offset) * 2
        if day.weekday() >= 5:
            base = int(base * 0.5)
        n_failures = max(1, int(rng.normal(base, 4)))
        if day_offset == 2:
            n_failures += 22  # injected anomaly spike

        for _ in range(n_failures):
            pipeline = rng.choice(PIPELINES, p=[0.32, 0.26, 0.18, 0.14, 0.10])
            activity_name, activity_type = ACTIVITIES[pipeline][
                rng.integers(0, len(ACTIVITIES[pipeline]))
            ]
            error_msg = rng.choice(ERROR_MESSAGES, p=[0.28, 0.18, 0.17, 0.15, 0.10, 0.07, 0.05])
            ts = day.replace(
                hour=int(rng.integers(0, 23)), minute=int(rng.integers(0, 59)),
                second=0, microsecond=0,
            )
            records.append({
                "TimeGenerated": ts, "PipelineName": pipeline, "ActivityName": activity_name,
                "ActivityType": activity_type, "Status": "Failed", "Error": error_msg,
                "PipelineRunId": f"run-{run_id}",
            })
            run_id += 1

    return pd.DataFrame(records).sort_values("TimeGenerated", ascending=False).reset_index(drop=True)


def build_kql_query(lookback_days: int) -> str:
    """KQL query against standard ADF diagnostic-log tables in Log Analytics."""
    return f"""
ADFActivityRun
| where TimeGenerated >= ago({lookback_days}d)
| where Status == "Failed"
| project TimeGenerated, PipelineName, ActivityName, ActivityType, Status, Error, PipelineRunId
| order by TimeGenerated desc
""".strip()


def build_fabric_kql_query(lookback_days: int) -> str:
    """
    KQL for Fabric workspace monitoring (Fabric pipeline/notebook diagnostic
    logs routed to a Log Analytics workspace). Column names are aliased into
    the same schema the analytics engine expects.
    """
    return f"""
FabricPipelineRunLogs
| where TimeGenerated >= ago({lookback_days}d)
| where Status =~ "Failed"
| project TimeGenerated,
          PipelineName = ItemName,
          ActivityName = OperationName,
          ActivityType = ItemKind,
          Status,
          Error = ErrorMessage,
          PipelineRunId = JobInstanceId
| order by TimeGenerated desc
""".strip()


# ============================================================================
# ANALYTICS ENGINE
# ============================================================================
def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Adds derived columns: date, error category, recommended action, is_notebook."""
    df = df.copy()
    df["TimeGenerated"] = pd.to_datetime(df["TimeGenerated"])
    df["Date"] = df["TimeGenerated"].dt.date
    cats = df["Error"].apply(categorize_error)
    df["ErrorCategory"] = cats.apply(lambda x: x[0])
    df["RecommendedAction"] = cats.apply(lambda x: x[1])
    df["IsNotebook"] = df["ActivityType"].isin(NOTEBOOK_ACTIVITY_TYPES)
    df["ErrorSignature"] = df["PipelineName"] + " | " + df["ActivityName"] + " | " + df["ErrorCategory"]
    return df


@dataclass
class Analysis:
    today_failures: int
    yesterday_failures: int
    dod_delta: int
    resolved: int
    unresolved: int
    carried_forward_df: pd.DataFrame
    new_count: int
    recurring_count: int
    new_vs_recurring_df: pd.DataFrame
    daily_trend: pd.Series
    anomaly_dates: list
    root_cause_df: pd.DataFrame
    overview_metrics: dict
    top_pipelines: pd.Series
    top_activities: pd.Series
    detail_df: pd.DataFrame


def analyze(df: pd.DataFrame) -> Analysis:
    df = enrich(df)

    today = df["Date"].max()
    yesterday = today - dt.timedelta(days=1)

    today_failures = int((df["Date"] == today).sum())
    yesterday_failures = int((df["Date"] == yesterday).sum())
    dod_delta = today_failures - yesterday_failures

    sig_by_day = df.groupby(["ErrorSignature", "Date"]).size().unstack(fill_value=0)
    unresolved_sigs = sig_by_day[sig_by_day[today] > 0].index if today in sig_by_day.columns else []
    unresolved = df[df["ErrorSignature"].isin(unresolved_sigs)]["ErrorSignature"].nunique()
    resolved = df["ErrorSignature"].nunique() - unresolved

    prior_sigs = set(df[df["Date"] < today]["ErrorSignature"])
    carried_today = df[(df["Date"] == today) & (df["ErrorSignature"].isin(prior_sigs))]
    carried_forward_df = (
        carried_today.drop_duplicates("ErrorSignature")
        [["PipelineName", "ActivityName", "ErrorCategory"]]
        .rename(columns={"ErrorCategory": "Error"})
        .reset_index(drop=True)
    )

    first_seen = df.groupby("ErrorSignature")["Date"].min()
    df["FirstSeen"] = df["ErrorSignature"].map(first_seen)
    today_rows = df[df["Date"] == today]
    new_count = today_rows[today_rows["FirstSeen"] == today]["ErrorSignature"].nunique()
    recurring_count = today_rows[today_rows["FirstSeen"] != today]["ErrorSignature"].nunique()
    new_vs_recurring_df = pd.DataFrame(
        {"Type": ["New", "Recurring"], "Count": [new_count, recurring_count]}
    ).set_index("Type")

    daily_trend = df.groupby("Date").size().sort_index()
    anomaly_dates = []
    if len(daily_trend) >= 4:
        mean, std = daily_trend.mean(), daily_trend.std(ddof=0)
        if std > 0:
            z = (daily_trend - mean) / std
            anomaly_dates = list(daily_trend[z > 1.5].index)

    root_cause_df = (
        df.groupby(["ErrorCategory", "RecommendedAction"])
        .size().reset_index(name="Occurrences")
        .sort_values("Occurrences", ascending=False)
        .rename(columns={"ErrorCategory": "Root Cause", "RecommendedAction": "Recommended Action"})
        .reset_index(drop=True)
    )

    overview_metrics = {
        "Failed Runs": int(df["PipelineRunId"].nunique()) if "PipelineRunId" in df else len(df),
        "Failed Pipes": int(df["PipelineName"].nunique()),
        "Failed Actv": int(df["ActivityName"].nunique()),
        "Unique Errors": int(df["ErrorCategory"].nunique()),
    }

    top_pipelines = df["PipelineName"].value_counts()
    top_activities = df["ActivityName"].value_counts()

    return Analysis(
        today_failures=today_failures, yesterday_failures=yesterday_failures, dod_delta=dod_delta,
        resolved=resolved, unresolved=unresolved, carried_forward_df=carried_forward_df,
        new_count=new_count, recurring_count=recurring_count, new_vs_recurring_df=new_vs_recurring_df,
        daily_trend=daily_trend, anomaly_dates=anomaly_dates, root_cause_df=root_cause_df,
        overview_metrics=overview_metrics, top_pipelines=top_pipelines, top_activities=top_activities,
        detail_df=df,
    )
