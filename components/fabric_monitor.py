"""
🏭 Microsoft Fabric Monitoring tab.

Uses the SAME Microsoft Entra ID sign-in as the ADF views (services.auth),
requesting only an additional Fabric-scoped token behind the scenes.

Flow: Workspace -> Item (DataPipeline / Notebook / Spark job) -> Job instances
      -> run detail, all pulled live from the Fabric REST API with the
      interactively signed-in user's own permissions.

Sub-tabs
--------
  📈 Overview          workspace inventory + run health at a glance
  ⚙️ Pipeline Runs     job-instance history for data pipelines
  📓 Notebook Runs     job-instance history for notebooks / Spark job definitions
  🏋️ Capacities        capacity SKU / state inventory
  🧯 Error Analytics   reuses the ADF analytics + remediation + alerting engine

Remediation steps are LLM-generated from each job's real `failureReason`
(flattened by fabric_api.failure_message); the static playbooks are the fallback.

Read-only: nothing in this tab can start, cancel or modify a Fabric job.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from services.alerting import dispatch_escalations, find_escalations, ESCALATION_MIN_DAYS
from services.analytics import analyze
from services.fabric_api import (
    NOTEBOOK_KINDS,
    PIPELINE_KINDS,
    get_job_instance,
    jobs_to_analytics_frame,
    jobs_to_dataframe,
    list_capacities,
    list_items,
    list_job_instances,
    list_workspaces,
)
from services.redaction import redact, redact_text
from services.remediation import (
    build_remediation_plan, plan_summary_df, plan_to_csv, plan_to_markdown,
)
from services.llm_remediation import clear_cache, llm_enabled, provider_label

_STATUS_ICON = {
    "Completed": "✅",
    "Succeeded": "✅",
    "Failed": "❌",
    "Cancelled": "🚫",
    "Deduped": "🚫",
    "InProgress": "🔄",
    "NotStarted": "⏳",
}


def _secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# Cached loaders
# --------------------------------------------------------------------------- #

def _get_workspaces():
    if "fb_workspaces" not in st.session_state:
        with st.spinner("Resolving Fabric workspaces..."):
            result = list_workspaces()
        if result.get("error"):
            st.error(f"Failed to load Fabric workspaces: {result['error']}")
            return []
        st.session_state.fb_workspaces = sorted(
            result.get("value", []), key=lambda w: (w.get("displayName") or "").lower()
        )
    return st.session_state.fb_workspaces


def _get_items(workspace_id: str):
    cache = st.session_state.setdefault("fb_items_cache", {})
    if workspace_id not in cache:
        with st.spinner("Loading workspace items..."):
            result = list_items(workspace_id)
        if result.get("error"):
            st.error(f"Failed to load items: {result['error']}")
            return None
        cache[workspace_id] = sorted(
            result.get("value", []), key=lambda i: (i.get("displayName") or "").lower()
        )
    return cache[workspace_id]


def _get_jobs(workspace_id: str, item_id: str):
    cache = st.session_state.setdefault("fb_jobs_cache", {})
    key = f"{workspace_id}:{item_id}"
    if key not in cache:
        with st.spinner("Loading job instances..."):
            result = list_job_instances(workspace_id, item_id)
        if result.get("error"):
            st.error(f"Failed to load job instances: {result['error']}")
            return None
        cache[key] = result.get("value", [])
    return cache[key]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def render_fabric_monitor_section():
    st.header("🏭 Microsoft Fabric Monitoring")
    st.caption(
        "Live Fabric workspace, pipeline, notebook and capacity monitoring via the Fabric "
        "REST API — signed in with the same Microsoft Entra ID account used for the ADF tabs."
    )

    if not st.session_state.get("authenticated") or not st.session_state.get("credential"):
        st.warning("Please authenticate from the **🔐 Authentication** tab to load Fabric data.")
        return

    col_a, col_b = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload workspaces", key="fb_reload_ws"):
            for k in ("fb_workspaces", "fb_items_cache", "fb_jobs_cache", "fb_capacities"):
                st.session_state.pop(k, None)
            st.rerun()

    workspaces = _get_workspaces()
    if not workspaces:
        st.info("No Fabric workspaces are accessible to your account.")
        return

    ws_options = {
        f"{w.get('displayName')}  ({w.get('id')})": w for w in workspaces
    }
    ws_label = st.selectbox("Fabric Workspace", list(ws_options.keys()), key="fb_ws_label")
    workspace = ws_options[ws_label]
    workspace_id = workspace.get("id")
    workspace_name = workspace.get("displayName", "")

    items = _get_items(workspace_id)
    if items is None:
        return
    if not items:
        st.info("This workspace has no items.")
        return

    tab_overview, tab_pipelines, tab_notebooks, tab_capacity, tab_errors = st.tabs(
        ["📈 Overview", "⚙️ Pipeline Runs", "📓 Notebook Runs", "🏋️ Capacities", "🧯 Error Analytics"]
    )

    with tab_overview:
        _render_overview(workspace, items)
    with tab_pipelines:
        _render_item_runs(workspace_id, items, PIPELINE_KINDS, "pipeline", key_prefix="fb_pl")
    with tab_notebooks:
        _render_item_runs(workspace_id, items, NOTEBOOK_KINDS, "notebook / Spark job",
                          key_prefix="fb_nb")
    with tab_capacity:
        _render_capacities()
    with tab_errors:
        _render_error_analytics(workspace_id, workspace_name, items)


# --------------------------------------------------------------------------- #
# 📈 Overview
# --------------------------------------------------------------------------- #

def _render_overview(workspace: dict, items: list[dict]):
    st.markdown("#### 📈 Workspace Overview")

    df = pd.DataFrame([
        {
            "Item": i.get("displayName", ""),
            "Type": i.get("type", ""),
            "Description": i.get("description", "") or "—",
            "Item Id": i.get("id", ""),
        }
        for i in items
    ])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Items", len(df))
    c2.metric("Data Pipelines", int((df["Type"].isin(PIPELINE_KINDS)).sum()))
    c3.metric("Notebooks", int((df["Type"].isin(NOTEBOOK_KINDS)).sum()))
    c4.metric("Lakehouses", int((df["Type"] == "Lakehouse").sum()))

    st.caption(
        f"Workspace: **{workspace.get('displayName')}**  ·  "
        f"Capacity: `{workspace.get('capacityId') or '—'}`  ·  "
        f"Type: {workspace.get('type', '—')}"
    )

    st.markdown("**Item mix**")
    st.bar_chart(df["Type"].value_counts())

    st.markdown("**All items**")
    st.dataframe(df, width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# ⚙️ / 📓 Run history
# --------------------------------------------------------------------------- #

def _render_item_runs(workspace_id: str, items: list[dict], kinds: set, label: str,
                      key_prefix: str):
    st.markdown(f"#### Run history — {label}")

    candidates = [i for i in items if i.get("type") in kinds]
    if not candidates:
        st.info(f"No {label} items in this workspace.")
        return

    opt = {f"{i.get('displayName')}  ·  {i.get('type')}": i for i in candidates}
    choice = st.selectbox("Item", list(opt.keys()), key=f"{key_prefix}_item")
    item = opt[choice]
    item_id = item.get("id")
    item_name = item.get("displayName", "")
    item_kind = item.get("type", "")

    col_a, _ = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload runs", key=f"{key_prefix}_reload_{item_id}"):
            st.session_state.setdefault("fb_jobs_cache", {}).pop(f"{workspace_id}:{item_id}", None)

    jobs = _get_jobs(workspace_id, item_id)
    if jobs is None:
        return
    if not jobs:
        st.info(f"No job instances found for **{item_name}**.")
        return

    statuses = sorted({j.get("status", "") for j in jobs})
    picked = st.multiselect("Filter status", statuses, default=statuses,
                            key=f"{key_prefix}_status")
    jobs_f = [j for j in jobs if j.get("status") in picked]

    total = len(jobs_f)
    failed = sum(1 for j in jobs_f if j.get("status") == "Failed")
    completed = sum(1 for j in jobs_f if j.get("status") in ("Completed", "Succeeded"))
    running = sum(1 for j in jobs_f if j.get("status") in ("InProgress", "NotStarted"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runs", total)
    c2.metric("Completed", completed)
    c3.metric("Failed", failed)
    c4.metric("In Progress", running)

    df = jobs_to_dataframe(jobs_f, item_name, item_kind)
    if not df.empty:
        df["Error"] = df["Error"].apply(redact_text)
    st.dataframe(df, width="stretch", hide_index=True)

    if not df.empty and df["Duration (s)"].notna().any():
        st.markdown("**Duration trend (seconds)**")
        trend = df.dropna(subset=["Duration (s)"]).set_index("Start (UTC)")["Duration (s)"]
        st.line_chart(trend)

    # ---- drill-down -------------------------------------------------------
    st.markdown("##### 🔍 Run detail")
    run_opt = {
        f"{_STATUS_ICON.get(j.get('status'), '🔹')} {(j.get('startTimeUtc') or '')[:19]} UTC  |  "
        f"{j.get('status')}  |  {j.get('id')}": j
        for j in jobs_f
    }
    if not run_opt:
        return

    run_label = st.selectbox("Job instance", list(run_opt.keys()), key=f"{key_prefix}_run")
    job = run_opt[run_label]

    if st.button("Load full job detail", key=f"{key_prefix}_detail_btn"):
        result = get_job_instance(workspace_id, item_id, job.get("id"))
        if result.get("error"):
            st.error(f"Failed to load job detail: {result['error']}")
        else:
            detail = result["value"][0]
            st.json(redact(detail), expanded=True)

    reason = job.get("failureReason")
    if reason:
        st.error(f"**Failure reason:** {redact_text(str(reason))}")


# --------------------------------------------------------------------------- #
# 🏋️ Capacities
# --------------------------------------------------------------------------- #

def _render_capacities():
    st.markdown("#### 🏋️ Fabric Capacities")
    st.caption(
        "SKU, state and region for every capacity you can see. For CU-level utilisation and "
        "throttling history, use the Fabric Capacity Metrics app — those counters are not "
        "exposed on the public REST API."
    )

    if "fb_capacities" not in st.session_state:
        with st.spinner("Loading capacities..."):
            result = list_capacities()
        if result.get("error"):
            st.error(f"Failed to load capacities: {result['error']}")
            return
        st.session_state.fb_capacities = result.get("value", [])

    caps = st.session_state.fb_capacities
    if not caps:
        st.info("No capacities visible to your account.")
        return

    df = pd.DataFrame([
        {
            "Capacity": c.get("displayName", ""),
            "SKU": c.get("sku", ""),
            "State": c.get("state", ""),
            "Region": c.get("region", ""),
            "Capacity Id": c.get("id", ""),
        }
        for c in caps
    ])

    c1, c2, c3 = st.columns(3)
    c1.metric("Capacities", len(df))
    c2.metric("Active", int((df["State"] == "Active").sum()))
    c3.metric("Distinct SKUs", df["SKU"].nunique())

    st.dataframe(df, width="stretch", hide_index=True)
    st.markdown("**Capacity mix by SKU**")
    st.bar_chart(df["SKU"].value_counts())


# --------------------------------------------------------------------------- #
# 🧯 Error Analytics (reuses the ADF engine)
# --------------------------------------------------------------------------- #

def _render_error_analytics(workspace_id: str, workspace_name: str, items: list[dict]):
    st.markdown("#### 🧯 Fabric Error Analytics & Remediation")
    st.caption(
        "Scans job history across the selected items, normalises Fabric failures into the "
        "same schema as ADF, and runs the identical root-cause / recurrence / remediation engine."
    )

    monitorable = [i for i in items if i.get("type") in (PIPELINE_KINDS | NOTEBOOK_KINDS)]
    if not monitorable:
        st.info("No pipelines or notebooks to analyse in this workspace.")
        return

    names = {f"{i.get('displayName')} · {i.get('type')}": i for i in monitorable}
    picked = st.multiselect(
        "Items to scan",
        list(names.keys()),
        default=list(names.keys())[:10],
        key="fb_ea_items",
        help="Each item costs one REST call. Start small on large workspaces.",
    )

    if st.button("🔍 Run Fabric analysis", type="primary", key="fb_ea_run"):
        frames = []
        progress = st.progress(0.0)
        for n, label in enumerate(picked, 1):
            item = names[label]
            result = list_job_instances(workspace_id, item.get("id"))
            if not result.get("error"):
                frames.append(jobs_to_analytics_frame(
                    result.get("value", []), item.get("displayName", ""), item.get("type", "")
                ))
            progress.progress(n / max(len(picked), 1))
        progress.empty()

        raw = pd.concat([f for f in frames if not f.empty], ignore_index=True) if frames else pd.DataFrame()

        if raw.empty:
            st.success("✅ No failed job instances found across the selected items. 🎉")
            st.session_state.fb_analysis = None
        else:
            st.session_state.fb_analysis = analyze(raw)

    analysis = st.session_state.get("fb_analysis")
    if analysis is None:
        st.info("Select items and click **Run Fabric analysis**.")
        return

    df = analysis.detail_df.copy()
    df["Error"] = df["Error"].apply(redact_text)

    c1, c2, c3 = st.columns(3)
    c1.metric("Today's Failures", analysis.today_failures)
    c2.metric("Yesterday", analysis.yesterday_failures)
    c3.metric("Day-over-Day", f"{analysis.dod_delta:+d}", delta=analysis.dod_delta)

    c1, c2 = st.columns(2)
    c1.metric("🆕 New", analysis.new_count)
    c2.metric("🔁 Recurring", analysis.recurring_count)

    st.markdown("**Failure trend**")
    trend = analysis.daily_trend.copy()
    trend.index = [d.strftime("%a %m/%d") for d in trend.index]
    st.line_chart(trend)

    for d in analysis.anomaly_dates:
        st.warning(f"⚠️ **Anomaly detected** on {d.strftime('%Y-%m-%d')} — "
                   "failure count significantly above baseline.")

    st.markdown("**Root cause summary**")
    st.dataframe(analysis.root_cause_df, width="stretch", hide_index=True)

    st.markdown("**Failure detail**")
    st.dataframe(
        df[["TimeGenerated", "PipelineName", "ActivityName", "ErrorCategory", "Error"]]
          .rename(columns={"PipelineName": "Item", "ActivityName": "Job Type",
                           "ErrorCategory": "Root Cause"})
          .sort_values("TimeGenerated", ascending=False),
        width="stretch", hide_index=True,
    )

    # ---- remediation (LLM-generated) --------------------------------------
    st.markdown("##### 🛠️ Remediation plan for recurring Fabric failures")
    st.caption(
        "Steps are written from each job's real `failureReason`, so notebook failures get "
        "Spark-level diagnosis and pipeline failures get activity/config diagnosis."
    )

    rc1, rc2, rc3 = st.columns([3, 2, 1])
    with rc1:
        min_days = st.slider(
            "Treat a failure as *recurring* when it appears on at least this many distinct days",
            min_value=2, max_value=7, value=2, key="fb_remediation_min_days",
        )
    with rc2:
        ai_available = llm_enabled()
        use_llm = st.toggle(
            "🤖 AI-generated remediation steps",
            value=ai_available,
            disabled=not ai_available,
            key="fb_remediation_use_llm",
            help=("Reads each Fabric job's real failureReason and writes a grounded playbook. "
                  "Falls back to the static playbooks if a call fails."
                  if ai_available else
                  "No LLM provider configured — set LLM_PROVIDER in .streamlit/secrets.toml."),
        )
        st.caption(f"Provider: {provider_label()}")
    with rc3:
        if st.button("↻ Regenerate", width="stretch", key="fb_remediation_regen",
                     help="Clear the AI cache and rebuild every playbook"):
            clear_cache()
            st.rerun()

    if not ai_available:
        st.info(
            "Running on **static fallback playbooks**. Add `LLM_PROVIDER` and credentials to "
            "`.streamlit/secrets.toml` to generate steps from the real error text.",
            icon="ℹ️",
        )

    with st.spinner("Building Fabric remediation plan..."):
        plan_items = build_remediation_plan(
            analysis.detail_df,
            min_days=min_days,
            use_llm=use_llm,
            platform="Microsoft Fabric",
        )

    if not plan_items:
        st.success(f"✅ No Fabric failure recurred on {min_days}+ distinct days. 🎉")
        return

    ai_n = sum(1 for i in plan_items if i.plan_source == "llm")

    k1, k2, k3 = st.columns(3)
    k1.metric("Recurring issues", len(plan_items))
    k2.metric("🔴 Critical", sum(1 for i in plan_items if "Critical" in i.severity))
    k3.metric("Still failing today", sum(1 for i in plan_items if i.still_active_today))

    st.caption(
        f"🤖 {ai_n} of {len(plan_items)} playbook(s) AI-generated from the raw error text  ·  "
        f"📘 {len(plan_items) - ai_n} using the static fallback."
    )

    st.dataframe(plan_summary_df(plan_items), width="stretch", hide_index=True)

    for idx, it in enumerate(plan_items, 1):
        badge = "🤖" if it.plan_source == "llm" else "📘"
        header = (f"{badge}  {it.severity}  ·  {it.pipeline} / {it.activity}  ·  "
                  f"{it.error_category}  ·  {it.occurrences}x over {it.days_affected} day(s)")
        with st.expander(header, expanded=(idx == 1)):
            c1, c2, c3 = st.columns(3)
            c1.markdown(f"**Owner:** {it.owner}")
            c2.markdown(f"**Target date:** {it.target_date.isoformat()}")
            c3.markdown(f"**Still failing today:** {'Yes' if it.still_active_today else 'No'}")

            st.markdown(f"**First → last seen:** {it.first_seen} → {it.last_seen}")
            st.markdown(
                f"**Root cause:** {it.root_cause}"
                + (f"  ·  _confidence: {it.confidence}_" if it.confidence else "")
            )

            if it.sample_errors:
                with st.popover("🔎 View the error text this plan was built from"):
                    for e in it.sample_errors:
                        st.code(redact_text(e), language="text")

            st.markdown("**Remediation steps:**")
            for n, step in enumerate(it.steps, 1):
                st.markdown(f"{n}. {step}")

            if it.validation:
                st.success(f"**Validation:** {it.validation}", icon="✅")

            if it.preventive:
                st.markdown("**Preventive actions (stop it recurring):**")
                for p in it.preventive:
                    st.markdown(f"- {p}")

    dl1, dl2 = st.columns(2)
    dl1.download_button(
        "📥 Download plan (Markdown)",
        data=plan_to_markdown(plan_items, generated_for=f"Fabric · {workspace_name}").encode("utf-8"),
        file_name="Fabric_Recurring_Remediation_Plan.md",
        mime="text/markdown",
        width="stretch",
    )
    dl2.download_button(
        "📥 Download plan (CSV — one row per step)",
        data=plan_to_csv(plan_items),
        file_name="Fabric_Recurring_Remediation_Plan.csv",
        mime="text/csv",
        width="stretch",
    )

    # ---- escalation -------------------------------------------------------
    st.markdown("##### 🚨 Escalation for Critical persistent Fabric failures")
    esc_days = st.slider(
        "Escalate when a Critical item has failed on at least N days",
        min_value=3, max_value=14, value=ESCALATION_MIN_DAYS, key="fb_esc_days",
    )
    candidates = find_escalations(plan_items, min_days=esc_days, critical_only=True)
    if not candidates:
        st.success(f"✅ No Critical Fabric failure has persisted {esc_days}+ days.")
        return

    st.error(f"🚨 {len(candidates)} Critical issue(s) persisted {esc_days}+ days and qualify for alerting.")
    st.dataframe(
        [{"Item": c.pipeline, "Job": c.activity, "Days": c.days_affected,
          "Root cause": c.error_category, "Owner": c.owner,
          "Plan": "🤖 AI" if c.plan_source == "llm" else "📘 Static"} for c in candidates],
        width="stretch", hide_index=True,
    )

    teams_webhook = st.text_input("Teams webhook URL", type="password",
                                  value=_secret("TEAMS_WEBHOOK", ""), key="fb_webhook")
    recipients = st.text_input("Email recipients (comma-separated)",
                               value=_secret("ALERT_RECIPIENTS", ""), key="fb_recipients").split(",")

    colA, colB = st.columns(2)
    if colA.button("📨 Send alerts now", key="fb_send"):
        results = dispatch_escalations(
            plan_items, min_days=esc_days,
            factory=f"Fabric · {workspace_name}",
            teams_webhook=teams_webhook.strip(),
            credential=st.session_state.get("credential"),
            recipients=[r for r in recipients if r.strip()],
            force=True,
        )
        for r in results:
            (st.success if (r.email_ok or r.teams_ok) else st.warning)(
                f"{r.pipeline}: email={r.email_detail} · teams={r.teams_detail}")
    colB.caption("Auto-send fires once per issue per day (throttled).")
