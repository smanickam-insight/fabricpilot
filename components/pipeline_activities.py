"""
Pipeline Activities page/tab for Azure Data Factory.

Flow: Subscription -> Data Factory -> published Pipeline -> Pipeline Run ->
activity-level logs, all pulled live from the ADF REST API with the
interactively signed-in user's own permissions.

Activity input/output payloads are redacted by default (SAS tokens, auth
headers, connection strings) and raw payloads sit behind an explicit toggle.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from services.adf_api import (
    list_subscriptions,
    list_factories,
    list_pipelines,
    query_pipeline_runs,
    query_activity_runs,
    parse_factory_id,
)
from services.redaction import redact, redact_url

_STATUS_ICON = {
    "Succeeded": "✅",
    "Success": "✅",
    "Completed": "✅",
    "Failed": "❌",
    "Cancelled": "🚫",
    "Canceled": "🚫",
    "Cancelling": "🚫",
    "InProgress": "🔄",
    "Running": "🔄",
    "NotStarted": "⏳",
    "Queued": "⏳",
}

_STATUS_COLOR = {
    "Succeeded": "green",
    "Completed": "green",
    "Failed": "red",
    "Cancelled": "orange",
    "Canceled": "orange",
    "Cancelling": "orange",
    "InProgress": "blue",
    "Running": "blue",
}

# ADF only retains pipeline run data for 45 days.
MAX_LOOKBACK_DAYS = 45


def render_pipeline_activities_section():
    """Render the Pipeline Activities tab."""
    st.header("🧩 ADF Pipeline Activities")
    st.caption(
        "Inspect activity-level execution details for a specific Data Factory pipeline run, "
        "pulled live from the Azure Data Factory REST API. Run history is retained for 45 days."
    )

    if not st.session_state.get("authenticated") or not st.session_state.get("credential"):
        st.warning("Please authenticate from the **Authentication** tab to load your subscriptions.")
        return

    # ---------------- Subscription selection ----------------
    subscriptions = _get_subscriptions()
    if not subscriptions:
        return

    sub_options = {
        f"{s.get('displayName')}  ({s.get('subscriptionId')})": s for s in subscriptions
    }
    sub_label = st.selectbox(
        "Subscription",
        options=list(sub_options.keys()),
        key="pa_subscription_label",
        help="Azure subscriptions your account has access to in this tenant.",
    )
    if not sub_label:
        return
    subscription_id = sub_options[sub_label]["subscriptionId"]

    # ---------------- Data Factory selection ----------------
    factories = _get_factories(subscription_id)
    if factories is None:
        return
    if not factories:
        st.info("No Data Factories found in this subscription.")
        return

    fac_options = {
        f"{f.get('name')}  ·  {parse_factory_id(f.get('id', ''))[1]}  ·  {f.get('location', '')}": f
        for f in factories
    }
    fac_label = st.selectbox(
        "Data Factory",
        options=list(fac_options.keys()),
        key="pa_factory_label",
        help="Shown as: factory name · resource group · region.",
    )
    if not fac_label:
        return

    factory = fac_options[fac_label]
    sub_id, resource_group, factory_name = parse_factory_id(factory["id"])

    # ---------------- Published pipeline selection ----------------
    pipelines = _get_pipelines(sub_id, resource_group, factory_name)
    if pipelines is None:
        return
    if not pipelines:
        st.info("No published pipelines found in this factory.")
        return

    st.success(f"**{len(pipelines)}** published pipeline(s) in `{factory_name}`")

    with st.expander("📚 All published pipelines", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Pipeline": p.get("name", ""),
                        "Folder": (p.get("properties", {}).get("folder") or {}).get("name", "—"),
                        "Activities": len(p.get("properties", {}).get("activities", []) or []),
                        "Description": p.get("properties", {}).get("description", "") or "—",
                    }
                    for p in pipelines
                ]
            ),
            width="stretch",
            hide_index=True,
        )

    pl_options = {p.get("name", ""): p for p in pipelines}
    pl_name = st.selectbox(
        "Pipeline",
        options=list(pl_options.keys()),
        key="pa_pipeline_label",
    )
    if not pl_name:
        return

    st.divider()

    # ---------------- Pipeline run selection ----------------
    lookback = st.slider(
        "Run history window (days)",
        min_value=1,
        max_value=MAX_LOOKBACK_DAYS,
        value=7,
        key="pa_lookback_days",
        help="ADF retains pipeline run data for 45 days. Use Azure Monitor diagnostic logs "
             "for longer retention.",
    )

    runs = _get_pipeline_runs(sub_id, resource_group, factory_name, pl_name, lookback)
    if runs is None:
        return
    if not runs:
        st.info(f"No runs found for **{pl_name}** in the last {lookback} day(s).")
        return

    runs_sorted = sorted(runs, key=lambda r: r.get("runStart") or "", reverse=True)
    run_options = {_format_run_label(r): r for r in runs_sorted}
    run_label = st.selectbox(
        "Pipeline Run (Run ID + Date)",
        options=list(run_options.keys()),
        key="pa_run_label",
        help="Each option shows status, start time, and the Run ID.",
    )
    if not run_label:
        return

    selected_run = run_options[run_label]
    _render_run_summary(selected_run)

    st.divider()
    st.subheader("🎬 Activity Runs")
    _render_activity_runs(sub_id, resource_group, factory_name, selected_run)


# ---------------- Data loaders (with simple session caching) ----------------
def _get_subscriptions() -> list:
    col_a, col_b = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload subscriptions", key="pa_reload_subs"):
            for key in ("pa_subscriptions", "pa_factories_cache",
                        "pa_pipelines_cache", "pa_runs_cache", "pa_activity_cache"):
                st.session_state.pop(key, None)

    if "pa_subscriptions" not in st.session_state:
        with st.spinner("Resolving subscriptions..."):
            result = list_subscriptions()
        if result.get("error"):
            st.error(f"Failed to load subscriptions: {result['error']}")
            return []
        st.session_state.pa_subscriptions = sorted(
            result.get("value", []),
            key=lambda s: (s.get("displayName") or "").lower(),
        )

    subs = st.session_state.pa_subscriptions
    if not subs:
        st.info("No Azure subscriptions are accessible to your account in this tenant.")
    return subs


def _get_factories(subscription_id: str):
    cache = st.session_state.setdefault("pa_factories_cache", {})
    if subscription_id not in cache:
        with st.spinner("Resolving Data Factories..."):
            result = list_factories(subscription_id)
        if result.get("error"):
            st.error(f"Failed to load Data Factories: {result['error']}")
            return None
        cache[subscription_id] = sorted(
            result.get("value", []),
            key=lambda f: (f.get("name") or "").lower(),
        )
    return cache[subscription_id]


def _get_pipelines(sub_id: str, resource_group: str, factory_name: str):
    cache = st.session_state.setdefault("pa_pipelines_cache", {})
    key = f"{sub_id}:{resource_group}:{factory_name}"

    col_a, _ = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload pipelines", key=f"pa_reload_pl_{factory_name}"):
            cache.pop(key, None)

    if key not in cache:
        with st.spinner("Loading published pipelines..."):
            result = list_pipelines(sub_id, resource_group, factory_name)
        if result.get("error"):
            st.error(f"Failed to load pipelines: {result['error']}")
            return None
        cache[key] = sorted(
            result.get("value", []),
            key=lambda p: (p.get("name") or "").lower(),
        )
    return cache[key]


def _get_pipeline_runs(sub_id: str, resource_group: str, factory_name: str,
                       pipeline_name: str, lookback_days: int):
    cache = st.session_state.setdefault("pa_runs_cache", {})
    key = f"{sub_id}:{resource_group}:{factory_name}:{pipeline_name}:{lookback_days}"

    col_a, _ = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload runs", key=f"pa_reload_runs_{pipeline_name}"):
            cache.pop(key, None)

    if key not in cache:
        now = datetime.now(timezone.utc)
        after = _fmt(now - timedelta(days=lookback_days))
        before = _fmt(now + timedelta(minutes=5))
        with st.spinner("Loading pipeline runs..."):
            result = query_pipeline_runs(
                sub_id, resource_group, factory_name, pipeline_name, after, before
            )
        if result.get("error"):
            st.error(f"Failed to load pipeline runs: {result['error']}")
            return None
        cache[key] = result.get("value", [])
    return cache[key]


# ---------------- Rendering helpers ----------------
def _format_run_label(run: dict) -> str:
    status = run.get("status", "Unknown")
    icon = _STATUS_ICON.get(status, "🔹")
    start = (run.get("runStart") or "")[:19].replace("T", " ")
    run_id = run.get("runId", "Unknown")
    return f"{icon} {start} UTC  |  {status}  |  {run_id}"


def _show_raw() -> bool:
    """Whether the user has explicitly opted into unredacted payloads."""
    return bool(st.session_state.get("pa_show_raw", False))


def _render_run_summary(run: dict):
    st.subheader("📊 Run Summary")
    col1, col2, col3, col4, col5 = st.columns(5)
    status = run.get("status", "Unknown")
    color = _STATUS_COLOR.get(status, "gray")
    dur_ms = run.get("durationInMs")

    with col1:
        st.markdown(f"**Status:** :{color}[{status}]")
    with col2:
        st.markdown(f"**Invoked by:** {(run.get('invokedBy') or {}).get('name', 'N/A')}")
    with col3:
        st.markdown(f"**Start (UTC):** {(run.get('runStart') or 'N/A')[:19]}")
    with col4:
        st.markdown(f"**End (UTC):** {(run.get('runEnd') or 'In Progress')[:19]}")
    with col5:
        st.markdown(f"**Duration:** {_humanize_ms(dur_ms)}")

    if run.get("message"):
        (st.error if status == "Failed" else st.info)(run["message"])

    with st.expander("📋 Run ID & parameters", expanded=False):
        st.code(run.get("runId", ""), language=None)
        if run.get("parameters"):
            st.markdown("**Parameters**")
            st.json(_maybe_redact(run["parameters"]), expanded=False)
        if run.get("runDimension"):
            st.markdown("**Run dimensions**")
            st.json(_maybe_redact(run["runDimension"]), expanded=False)


def _maybe_redact(payload):
    return payload if _show_raw() else redact(payload)


def _render_activity_runs(sub_id: str, resource_group: str, factory_name: str, run: dict):
    run_id = run.get("runId")
    if not run_id:
        st.info("This run has no run ID.")
        return

    last_after, last_before = _activity_query_window(run)
    cache = st.session_state.setdefault("pa_activity_cache", {})
    cache_key = f"{factory_name}:{run_id}:{last_after}:{last_before}"

    col_a, col_b = st.columns([1, 5])
    with col_a:
        if st.button("🔄 Reload activities", key=f"pa_reload_acts_{run_id}"):
            cache.pop(cache_key, None)
    with col_b:
        st.toggle(
            "Show raw (unredacted) payloads",
            key="pa_show_raw",
            help="Off by default. Activity input/output can contain SAS tokens, "
                 "Authorization headers and connection strings — avoid enabling "
                 "this while screen-sharing.",
        )

    if _show_raw():
        st.warning(
            "⚠️ Raw mode is on: secrets in activity payloads are displayed in full.",
            icon="⚠️",
        )

    if cache_key not in cache:
        with st.spinner("Querying activity runs..."):
            result = query_activity_runs(
                sub_id, resource_group, factory_name, run_id, last_after, last_before
            )
        if result.get("error"):
            st.error(f"Failed to load activity runs: {result['error']}")
            return
        cache[cache_key] = {
            "value": result.get("value", []),
            "pages_fetched": result.get("pages_fetched", 1),
            "truncated": result.get("truncated", False),
        }

    cached = cache[cache_key]
    activities = cached["value"]
    if not activities:
        st.info("No activity runs found for the selected pipeline run.")
        return

    if cached["pages_fetched"] > 1 or cached["truncated"]:
        msg = f"Fetched {len(activities)} activities across {cached['pages_fetched']} page(s)."
        if cached["truncated"]:
            st.warning(msg + " More results may exist — page cap reached.")
        else:
            st.caption(msg)

    # Stats
    total = len(activities)
    failed = sum(1 for a in activities if a.get("status") == "Failed")
    succeeded = sum(1 for a in activities if a.get("status") in ("Succeeded", "Completed"))
    in_progress = sum(
        1 for a in activities
        if a.get("status") in ("InProgress", "Running", "Queued", "NotStarted")
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Activities", total)
    c2.metric("Succeeded", succeeded)
    c3.metric("Failed", failed)
    c4.metric("In Progress", in_progress)

    # Table view
    df = pd.DataFrame(
        [
            {
                "Status": a.get("status", ""),
                "Activity": a.get("activityName", ""),
                "Type": a.get("activityType", ""),
                "Start (UTC)": (a.get("activityRunStart") or "")[:19].replace("T", " "),
                "End (UTC)": (a.get("activityRunEnd") or "")[:19].replace("T", " "),
                "Duration (ms)": a.get("durationInMs"),
                "Integration Runtime": _ir_name(a),
                "Activity Run ID": a.get("activityRunId", ""),
            }
            for a in activities
        ]
    )
    st.dataframe(df, width="stretch", hide_index=True)

    # Per-activity drill-down
    st.markdown("##### 🔍 Activity Details")
    for a in activities:
        status = a.get("status", "Unknown")
        icon = _STATUS_ICON.get(status, "🔹")
        name = a.get("activityName", "Unnamed")
        atype = a.get("activityType", "")
        dur_ms = a.get("durationInMs")

        with st.expander(
            f"{icon} {name}  ·  {atype}  ·  {status}  ·  {_humanize_ms(dur_ms)}", expanded=False
        ):
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Activity Run ID:** `{a.get('activityRunId', '')}`")
                st.markdown(f"**Start (UTC):** {a.get('activityRunStart', 'N/A')}")
                st.markdown(f"**End (UTC):** {a.get('activityRunEnd', 'N/A')}")
            with col2:
                st.markdown(f"**Linked Service:** {a.get('linkedServiceName') or '—'}")
                st.markdown(f"**Integration Runtime:** {_ir_name(a)}")
                st.markdown(
                    f"**Retry Attempt:** "
                    f"{a.get('retryAttempt') if a.get('retryAttempt') is not None else '—'}"
                )
                st.markdown(f"**Recovery Status:** {a.get('recoveryStatus', '—')}")

            err = a.get("error") or {}
            if err.get("message"):
                safe_err = err if _show_raw() else redact(err)
                st.error(
                    f"**Error ({safe_err.get('errorCode', '')} / "
                    f"{safe_err.get('failureType', '')}):** {safe_err.get('message', '')}"
                )
                if safe_err.get("target"):
                    st.caption(f"Target: {safe_err['target']}")

            _render_copy_metrics(a)
            _render_activity_urls(a)

            if a.get("input"):
                st.markdown("**Input**")
                st.json(_maybe_redact(a["input"]), expanded=False)
            if a.get("output"):
                st.markdown("**Output**")
                st.json(_maybe_redact(a["output"]), expanded=False)


def _render_copy_metrics(activity: dict):
    """Surface the headline metrics of a Copy activity, per the ADF monitoring docs."""
    if (activity.get("activityType") or "").lower() != "copy":
        return
    out = activity.get("output") or {}
    keys = [
        ("dataRead", "Data read (bytes)"),
        ("dataWritten", "Data written (bytes)"),
        ("rowsRead", "Rows read"),
        ("rowsCopied", "Rows copied"),
        ("rowsSkipped", "Rows skipped"),
        ("copyDuration", "Copy duration (s)"),
        ("throughput", "Throughput (KB/s)"),
    ]
    present = [(label, out[k]) for k, label in keys if out.get(k) is not None]
    if not present:
        return

    st.markdown("**📦 Copy metrics**")
    cols = st.columns(min(4, len(present)))
    for idx, (label, value) in enumerate(present):
        cols[idx % len(cols)].metric(
            label, f"{value:,}" if isinstance(value, (int, float)) else value
        )


def _ir_name(activity: dict) -> str:
    ir = activity.get("integrationRuntimeNames")
    if isinstance(ir, list) and ir:
        return ", ".join(ir)
    if isinstance(ir, str) and ir:
        return ir
    return "—"


def _humanize_ms(ms) -> str:
    if not isinstance(ms, (int, float)):
        return "—"
    seconds, millis = divmod(int(ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    if seconds:
        return f"{seconds}.{millis:03d}s"
    return f"{millis} ms"


def _activity_query_window(run: dict) -> tuple[str, str]:
    """
    Build the lastUpdatedAfter/Before window required by queryActivityruns.

    Uses the run's start/end with a small margin; falls back to "last 7 days
    ending now" if the timestamps are missing.
    """
    now = datetime.now(timezone.utc)
    start_dt = _parse_iso(run.get("runStart")) or (now - timedelta(days=7))
    end_dt = _parse_iso(run.get("runEnd")) or now
    return _fmt(start_dt - timedelta(minutes=1)), _fmt(end_dt + timedelta(minutes=1))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _parse_iso(value):
    if not value:
        return None
    try:
        v = value.rstrip("Z")
        if "." in v:
            head, frac = v.split(".", 1)
            frac = (frac + "000000")[:6]
            v = f"{head}.{frac}"
        return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


_URL_KEYS = {"monitorurl", "runurl", "resulturl", "notebookurl",
             "sparkuiurl", "logslink", "runpageurl"}

# Only these schemes are ever rendered as clickable links.
_SAFE_SCHEMES = ("http://", "https://")


def _collect_urls(obj, path="") -> list[tuple[str, str]]:
    """Walk a JSON-like structure and return [(label_path, url), ...]."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub_path = f"{path}.{k}" if path else k
            if isinstance(v, str) and v.startswith(_SAFE_SCHEMES):
                found.append((sub_path, v))
            elif isinstance(v, (dict, list)):
                found.extend(_collect_urls(v, sub_path))
            elif k.lower() in _URL_KEYS and isinstance(v, str) and v.startswith(_SAFE_SCHEMES):
                found.append((sub_path, v))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            found.extend(_collect_urls(item, f"{path}[{idx}]"))
    return found


def _escape_md(text: str) -> str:
    """Neutralise markdown control characters in interpolated link text."""
    for ch in ("\\", "[", "]", "(", ")", "<", ">", "`", "*", "_"):
        text = text.replace(ch, "\\" + ch)
    return text


def _render_activity_urls(activity: dict):
    """
    Render URLs found in the activity output (Databricks/Spark/monitor links).

    SAS signatures and auth codes are stripped from the query string unless
    raw mode is explicitly enabled.
    """
    urls = _collect_urls(activity.get("output") or {})
    seen, unique = set(), []
    for label, url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append((label, url))

    if not unique:
        return

    st.markdown("**🔗 Links**")
    for label, url in unique:
        display_url = url if _show_raw() else redact_url(url)
        key_name = label.split(".")[-1].lower()
        prefix = "📈 Monitor" if "monitor" in key_name else (
            "🧾 Run page" if "run" in key_name else f"`{_escape_md(label)}`"
        )
        st.markdown(f"- {prefix}: [{_escape_md(display_url)}]({display_url})")
