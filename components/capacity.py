"""Illustrative capacity analytics and a contextual, read-only details drawer."""

import altair as alt
import pandas as pd
import streamlit as st

from components.inspection import inspection_footer, inspection_panel


DETAILS = {
    "consumption": {
        "title": "CU consumption",
        "status": "Peak utilization review",
        "summary": "The sample capacity exceeds its 64 CU allocation during overlapping refresh and ingestion windows.",
        "logs": "14:00 UTC  semantic-refresh   38 CU\n14:00 UTC  lakehouse-ingest   26 CU\n14:00 UTC  interactive        14 CU\n14:00 UTC  total              78 CU",
        "steps": ["Inspect overlapping refresh schedules.", "Move noncritical ingestion outside the peak window.", "Compare interactive latency before changing the capacity tier."],
    },
    "smoothing": {
        "title": "Background compute smoothing",
        "status": "72% of sample smoothing budget",
        "summary": "Background operations are amortized across a 24-hour window. The illustrative budget has 28% headroom remaining.",
        "logs": "Window       24 hours\nUsed         72%\nHeadroom     28%\nTop workload nightly-delta-maintenance",
        "steps": ["Review carryforward and burndown in the Capacity Metrics app.", "Stagger large notebook and maintenance jobs.", "Review the Capacity Throttling Shield before enabling automation."],
    },
    "sizing": {
        "title": "F-SKU right-sizing",
        "status": "F64 retained / review pending",
        "summary": "The sample workload is not a downsize candidate: average consumption exceeds F32 and peak demand exceeds F64. Schedule optimization should precede a tier change.",
        "logs": "Current SKU      F64\nCandidate        Retain F64\nCost estimate    Not available\nBilling source   Not connected",
        "steps": ["Collect at least 14 days of representative utilization.", "Validate semantic model memory and concurrency requirements.", "Model reservation and regional pricing before approving a tier change."],
    },
    "delay": {
        "title": "Interactive delay detected",
        "status": "Warning / sample event",
        "summary": "Analytics Production entered an illustrative interactive delay window while ingestion and model refresh overlapped.",
        "logs": "2026-10-01 14:08 UTC\nCapacity  analytics-production\nEvent     interactive-delay\nDuration  42 seconds\nStatus    recovered",
        "steps": ["Identify high-CU operations in the affected window.", "Defer noncritical background jobs after owner approval.", "Confirm interactive latency returns to the agreed SLA."],
    },
    "burst": {
        "title": "Background burst detected",
        "status": "Warning / sample event",
        "summary": "A scheduled Spark ingestion job temporarily increased background demand. No job cancellation was performed.",
        "logs": "2026-10-01 13:42 UTC\nWorkload  sales-bronze-ingest\nEvent     background-burst\nPeak      78 CU\nAction    none",
        "steps": ["Inspect Spark executor memory and partition sizes.", "Review the Spark Out-of-Memory Auto-Tuning playbook.", "Test scheduling changes in a nonproduction workspace."],
    },
    "recovered": {
        "title": "Capacity returned to baseline",
        "status": "Resolved / sample event",
        "summary": "The sample capacity returned to normal after the scheduled refresh completed. This is not evidence of an automated repair.",
        "logs": "2026-10-01 12:16 UTC\nEvent     utilization-normalized\nStatus    resolved\nAction    natural recovery",
        "steps": ["Correlate the recovery with refresh completion.", "Record the incident duration and affected workloads.", "Review repeat occurrences before closing the investigation."],
    },
}


def _open_detail(topic):
    st.session_state.detail_topic = topic


def _close_detail():
    st.session_state.pop("detail_topic", None)


def _chart_selected():
    if st.session_state.cu_chart.selection.get("sample"):
        _open_detail("consumption")


def capacity_samples(period):
    values = [30, 28, 26, 25, 29, 35, 41, 48, 52, 49, 57, 63,
              58, 66, 78, 71, 60, 55, 50, 46, 43, 39, 35, 32]
    if period == "7 days":
        values = [43, 48, 45, 53, 58, 46, 41]
    elif period == "30 days":
        values = [36 + (index * 7 % 29) for index in range(30)]
    frame = pd.DataFrame({
        "Time": pd.date_range(end="2026-10-01 23:00", periods=len(values),
                              freq="h" if period == "24 hours" else "D"),
        "Consumption": values,
    })
    return frame


def capacity_projection(samples, target_cu, hourly_rate):
    return {"monthly_savings": (64 - target_cu) * hourly_rate * 730,
            "over_target": float((samples.Consumption > target_cu).mean()),
            "monthly_cost": target_cu * hourly_rate * 730}


def render_capacity_section():
    st.caption("OPERATIONS / CAPACITY MANAGEMENT")
    st.title("Capacity & Cost Optimization")
    st.caption("DEMO CAPACITY / Analytics Production / F64 / West Europe")
    timeframe = st.session_state.get("platform_timeframe", "Last 24h")
    period = {"Last 1h": "24 hours", "Last 24h": "24 hours", "Last 7d": "7 days", "Last 30d": "30 days"}[timeframe]
    samples = capacity_samples(period)
    samples["Time"] = samples.Time - pd.Timedelta(hours=8, minutes=30)
    if period == "24 hours":
        samples = samples.set_index("Time").resample("5min").interpolate().reset_index()
        if timeframe == "Last 1h":
            samples = samples.tail(13).copy()
    samples["Background"] = samples.Consumption.rolling(len(samples), min_periods=1).mean() * 0.58
    st.caption("Snapshot 01 Oct 2026, 14:30 UTC / synthetic samples / live telemetry and billing disconnected")

    metrics = [
        ("Average consumption", f"{samples.Consumption.mean():.1f} CU", "consumption", "monitoring"),
        ("Peak utilization", f"{samples.Consumption.max() / 64:.0%}", "consumption", "speed"),
        ("Background budget", "72%", "smoothing", "hourglass_bottom"),
        ("Current capacity", "F64", "sizing", "memory"),
    ]
    for column, (label, value, topic, icon) in zip(st.columns(4), metrics):
        with column.container(border=True, key=f"capacity_card_{icon}"):
            st.caption(label)
            st.button(value, key=f"metric_{label}", icon=f":material/{icon}:",
                      on_click=_open_detail, args=(topic,), width="stretch", help=f"Inspect {label.lower()}")

    chart_column, sizing_column = st.columns([2, 1], gap="medium")
    with chart_column.container(border=True, key="capacity_card_trend"):
        st.subheader("CU consumption & smoothing")
        unit = st.segmented_control("Display unit", ["Capacity units", "Estimated USD"], default="Capacity units", key="capacity_units") or "Capacity units"
        hourly_rate = st.number_input("Illustrative USD per CU-hour", min_value=0.0, max_value=10.0, value=0.18, step=0.01, key="capacity_rate")
        factor = hourly_rate if unit == "Estimated USD" else 1
        samples["Display total"] = samples.Consumption * factor
        samples["Display background"] = samples.Background * factor
        st.caption("Consumption-equivalent USD/hour, not the provisioned capacity bill" if unit == "Estimated USD" else "Simulated 5-minute peaks and smoothed background" if period == "24 hours" else "Daily averages / peaks within a day are not represented")
        selection = alt.selection_point(name="sample", fields=["Time"], nearest=True, on="click", clear="dblclick")
        base = alt.Chart(samples).encode(
            x=alt.X("Time:T", title=None, axis=alt.Axis(format="%H:%M" if period == "24 hours" else "%d %b", tickCount=6)),
            y=alt.Y("Display total:Q", title="USD/hour equivalent" if unit == "Estimated USD" else "Capacity units", scale=alt.Scale(domain=[0, max(90 * factor, 1)])),
            tooltip=[alt.Tooltip("Time:T", title="UTC"), alt.Tooltip("Consumption:Q", title="CU")],
        )
        area = base.mark_area(color="#159b85", opacity=0.24).encode(y="Display background:Q")
        line = base.mark_line(color="#1263df", strokeWidth=2.5)
        points = base.mark_circle(size=35, color="#1263df", opacity=0.05).add_params(selection)
        limit = alt.Chart(pd.DataFrame({"Allocation": [64 * factor]})).mark_rule(color="#d58a22", strokeDash=[5, 5]).encode(y="Allocation:Q")
        chart = (area + line + points + limit).properties(height=240).configure_view(strokeWidth=0)
        st.altair_chart(chart, key="cu_chart", on_select=_chart_selected, selection_mode=["sample"], width="stretch")
        st.button("Inspect consumption", icon=":material/query_stats:", on_click=_open_detail, args=("consumption",), key="inspect_consumption")

    with sizing_column.container(border=True, key="capacity_card_sizing"):
        st.subheader("F-SKU simulator")
        target = st.select_slider("Target SKU", options=[16, 32, 64, 128], value=32, format_func=lambda value: f"F{value}", key="capacity_target")
        projection = capacity_projection(samples, target, hourly_rate)
        st.metric("Illustrative monthly savings", f"${projection['monthly_savings']:,.0f}")
        st.metric("Samples above target", f"{projection['over_target']:.1%}")
        st.progress(projection["over_target"])
        st.caption(f"F64 to F{target} / 730 provisioned hours / ${hourly_rate:.2f} per CU-hour / no reservations")
        st.caption("Not an outage probability. Daily averages can hide short peaks. Memory and concurrency limits are not modeled.")
        st.button("Review recommendation", icon=":material/arrow_forward:", key="inspect_sizing",
                  on_click=_open_detail, args=("sizing",), width="stretch")

    smoothing_column, alerts_column = st.columns([1, 1], gap="medium")
    with smoothing_column.container(border=True, key="capacity_card_smoothing"):
        st.subheader("Background Compute Smoothing")
        st.metric("24-hour budget used", "72%", "28% headroom", delta_color="off")
        st.progress(72)
        st.caption("Sample 24-hour smoothing window / independent of chart range")
        st.button("Inspect background compute", icon=":material/tune:", on_click=_open_detail,
                  args=("smoothing",), key="inspect_smoothing")
    with alerts_column.container(border=True, key="capacity_card_alerts"):
        st.subheader("Burst & throttling timeline")
        st.caption("01 Oct 2026 / grouped synthetic events")
        for topic, label, icon in [
            ("delay", "14:08 UTC · Interactive delay · 42 sec", "warning"),
            ("burst", "13:42 UTC · Background burst · 78 CU", "bolt"),
            ("recovered", "12:16 UTC · Returned to baseline", "check_circle"),
        ]:
            if timeframe != "Last 1h" or topic in ("delay", "burst"):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.badge("Recovered" if topic == "recovered" else "Warning", color="green" if topic == "recovered" else "orange")
                    st.button(label, icon=f":material/{icon}:", key=f"alert_{topic}",
                              on_click=_open_detail, args=(topic,), width="stretch", wrap=True)
    render_details_drawer()


def render_details_drawer():
    topic = st.session_state.get("detail_topic")
    if topic not in DETAILS:
        return
    detail = DETAILS[topic]
    with inspection_panel(detail["title"], "Analytics Production / F64 / West Europe", "Demo insight", "Synthetic snapshot / 01 Oct 2026",
                          "close_details", _close_detail) as (root_cause, telemetry):
        with root_cause:
            st.write(f"**{detail['status']}**")
            st.write(detail["summary"])
            st.subheader("Recommended actions")
            for number, step in enumerate(detail["steps"], start=1):
                st.write(f"{number}. {step}")
            st.link_button("Capacity metrics reference", "https://learn.microsoft.com/fabric/enterprise/metrics-app", icon=":material/open_in_new:")
        with telemetry:
            st.code(detail["logs"], language="text", wrap_lines=True)
    with inspection_footer():
        with st.container(horizontal=True):
            st.button("Dismiss", icon=":material/close:", on_click=_close_detail)
            st.button("Apply capacity change", icon=":material/memory:", type="primary", disabled=True,
                      help="Requires live capacity telemetry, verified Azure write permissions and an approved budget.")
        st.caption("Advisory only / no changes applied")