"""Access-aware DataOps workspaces backed by the shared assessment engine."""

import json
from collections import Counter
from datetime import datetime, timezone
from html import escape

import altair as alt
from fpdf import FPDF
import pandas as pd
import streamlit as st

from services.access import Action, Resource, allowed
from services.agent_brain import ai_enabled, diagnose
from services.assessment import assessments, incidents, scorecard
from services.operations import PLAYBOOKS
from services.platform_session import refresh_live_session
from services.redaction import redact_text
from components.inspection import inspection_footer, inspection_panel


def _select_detail(detail_id):
    st.session_state.pilot_detail = detail_id
    st.session_state.pop("pilot_approved", None)


def _close_detail():
    st.session_state.pop("pilot_detail", None)


def _scope_changed(state_key, selector, permitted):
    selected = st.session_state[selector]
    st.session_state[state_key] = list(permitted) if selected == "all" else [selected]
    _close_detail()
    st.session_state.pop("detail_topic", None)


def render_scope_filter(principal):
    workspaces = {grant.workspace_id: grant.name for grant in principal.workspaces}
    capacities = {capacity: "Analytics Production F64" if principal.demo and capacity == "demo-f64" else capacity
                  for capacity in sorted(principal.capacity_ids)}
    with st.container(key="contextbar", horizontal=True, vertical_alignment="center", gap="small"):
        for label, choices, state_key, selector in [
            ("Capacity", capacities, "platform_capacities", "pilot_capacity_select"),
            ("Workspace", workspaces, "platform_workspaces", "pilot_workspace_select"),
        ]:
            options = {"all": "All capacities" if label == "Capacity" else "All workspaces", **choices}
            if st.session_state.get(selector, "all") not in options:
                st.session_state[selector] = "all"
            st.selectbox(label, list(options), format_func=options.get, key=selector,
                         label_visibility="collapsed", on_change=_scope_changed,
                         args=(state_key, selector, choices))
        st.selectbox("Time range", ["Last 1h", "Last 24h", "Last 7d", "Last 30d"], index=1,
                     key="platform_timeframe", label_visibility="collapsed")


def _table_action(key, ids):
    click = st.session_state.get(key)
    if click is not None and 0 <= click.row < len(ids):
        _select_detail(ids[click.row])


def _table_selection(key, ids):
    event = st.session_state.get(key)
    if event and event.selection.rows:
        row = event.selection.rows[0]
        if 0 <= row < len(ids):
            _select_detail(ids[row])


def action_table(rows, ids, key, action="Inspect", column_config=None):
    if not rows:
        st.info("No matching records in this scope.", icon=":material/check_circle:")
        return
    frame = pd.DataFrame(rows)
    frame["Action"] = action
    config = dict(column_config or {})
    if "Severity" in frame:
        frame["Severity"] = frame["Severity"].map(lambda severity: [severity])
        config["Severity"] = st.column_config.MultiselectColumn(
            "Severity", options=["Critical", "High", "Medium", "Low"],
            color=["#9f1239", "#dc3545", "#b7791f", "#167d68"], width="small")
    config["Action"] = st.column_config.ButtonColumn("Action", key=f"{key}_action",
        on_click=_table_action, args=(f"{key}_action", ids), width="small")
    st.dataframe(frame, hide_index=True, key=key, width="stretch", row_height=44,
                 height=min(480, 40 + len(rows) * 44), column_config=config,
                 on_select=lambda: _table_selection(key, ids), selection_mode="single-row")


def _finding_list(findings, key="pilot_findings"):
    action_table([{"Severity": finding["severity"], "Audit rule / finding": finding["title"],
                   "Target asset": finding["asset"], "Scope": finding["level"].title()}
                  for finding in findings], [finding["id"] for finding in findings], key,
                 action="Inspect & remediate")


def _incident_list(active, key="pilot_incidents"):
    action_table([{"Severity": incident["severity"], "Incident type": incident["title"],
                   "Target asset": incident["asset"],
                   "Duration": f"{incident['elapsed_seconds'] / 60:.0f} min" if incident.get("elapsed_seconds") else "Not collected",
                   "Status": "Open"} for incident in active],
                 [incident["id"] for incident in active], key, action="Triage")


def render_command_center(principal, records):
    results = assessments(principal, records)
    active = incidents(principal, records)
    score = scorecard(results)
    st.caption("DEMO ESTATE / snapshot 01 Oct 2026 / illustrative evidence" if principal.demo else "DELEGATED FABRIC ESTATE / scoped inventory")
    columns = st.columns(4, gap="small")
    with columns[0].container(border=True, key="pilot_card_health"):
        st.caption("Architecture health")
        percent = score["score"] or 0
        grade = score["grade"] if score["score"] is not None else "?"
        st.html(f'<div class="flex items-center gap-4"><div class="pilot-gauge" style="background:conic-gradient(#d65457 {percent}%,#edf0f5 0)"><span>{escape(grade)}</span></div><div><strong class="text-2xl font-semibold">{percent}/100</strong><p class="text-xs text-slate-500">{score["findings"]} open findings</p></div></div>')
        st.caption("No prior snapshot" if score["score"] is not None else "Not assessed")
    with columns[1].container(border=True, key="pilot_card_incidents"):
        st.metric("Active incidents", len(active))
        severity = Counter(incident["severity"] for incident in active)
        st.caption(" / ".join(f"{count} {level.lower()}" for level, count in severity.items()) or "No detected signals")
    with columns[2].container(border=True, key="pilot_card_savings"):
        st.metric("Monthly savings potential", "Unavailable")
        st.caption("Billing evidence not connected")
    with columns[3].container(border=True, key="pilot_card_coverage"):
        st.metric("Evidence coverage", f"{score['assessed']} / {score['total']}")
        passed = sum(result["status"] == "Pass" for result in results)
        st.caption(f"{passed} passed / {score['findings']} findings / {score['total'] - score['assessed']} unknown")
    left, right = st.columns([1, 1])
    with left:
        st.subheader("Architectural debt")
        by_level = []
        for level in ["tenant", "capacity", "workspace", "item"]:
            summary = scorecard([result for result in results if result["level"] == level])
            by_level.append({"Scope": level.title(), "Health": summary["score"], "Findings": summary["findings"], "Coverage": f"{summary['assessed']}/{summary['total']}"})
        st.dataframe(pd.DataFrame(by_level), hide_index=True, width="stretch", row_height=46,
                     column_config={"Health": st.column_config.ProgressColumn("Health", min_value=0, max_value=100, format="%d%%")})
        if st.button("Run full audit", icon=":material/fact_check:", key="pilot_run_audit"):
            st.session_state.sidebar_menu = "Architecture Audits"
            st.session_state.pilot_audit_time = datetime.now(timezone.utc).strftime("%H:%M UTC")
            st.rerun()
        st.caption("Assessment uses the latest available scoped snapshot.")
    with right:
        st.subheader("Urgent incidents")
        _incident_list(active[:5])
    render_context_drawer(principal, records, results, active)


def _workspace_name(principal, workspace_id):
    grant = principal.grant(workspace_id)
    return grant.name if grant else "Tenant / capacity"


def _filtered_findings(findings, prefix):
    with st.container(horizontal=True, vertical_alignment="bottom"):
        scope = st.pills("Scope", ["All", "Tenant", "Capacity", "Workspace", "Item"], default="All", key=f"{prefix}_scope") or "All"
        severity = st.selectbox("Severity", ["All severities", "Critical", "High", "Medium", "Low"], key=f"{prefix}_severity")
        query = st.text_input("Search findings", placeholder="Rule, asset or pillar", key=f"{prefix}_query").strip().casefold()
    return [finding for finding in findings if (scope == "All" or finding["level"].casefold() == scope.casefold())
            and (severity == "All severities" or finding["severity"] == severity)
            and query in (finding["title"] + " " + finding["asset"] + " " + finding["category"]).casefold()]


def _runtime_chart(active):
    workloads = {}
    for incident in active:
        if incident.get("baseline_seconds") and incident.get("elapsed_seconds"):
            workloads[incident["record_id"]] = incident
    rows = []
    for incident in workloads.values():
        baseline = incident["baseline_seconds"] / 60
        elapsed = incident["elapsed_seconds"] / 60
        state = "Critical >300%" if elapsed > baseline * 3 else "Warning >140%" if elapsed > baseline * 1.4 else "Within baseline"
        rows.append({"Workload": incident["asset"], "Elapsed": elapsed, "Baseline": baseline,
                     "Risk": state, "SLA warning": baseline * 1.4})
    if not rows:
        st.info("Runtime baselines have not been collected in this scope.")
        return
    chart = alt.Chart(pd.DataFrame(rows)).encode(y=alt.Y("Workload:N", title=None, axis=alt.Axis(labelLimit=180)))
    background = chart.mark_bar(color="#e9edf3", size=26).encode(x=alt.X("Elapsed:Q", title="Minutes since job start"))
    baseline = chart.mark_bar(color="#1b9e8a", size=8).encode(x="Baseline:Q")
    overrun = chart.mark_bar(size=20).encode(x="Baseline:Q", x2="Elapsed:Q", color=alt.Color("Risk:N", scale=alt.Scale(domain=["Critical >300%", "Warning >140%", "Within baseline"], range=["#d74759", "#e3a329", "#1b9e8a"])), tooltip=["Workload", "Elapsed", "Baseline", "Risk"])
    marker = chart.mark_tick(color="#29364a", size=34, thickness=2).encode(x="SLA warning:Q")
    st.altair_chart((background + baseline + overrun + marker).properties(height=max(140, len(rows) * 58)).configure_view(strokeWidth=0), width="stretch")
    st.caption("30-day baseline in teal / 140% duration marker / elapsed runtime through the snapshot")


def _governance(principal, records, findings):
    governance = [finding for finding in findings if finding["category"] == "Governance"]
    tenants = [record for record in records if record["kind"] == "Tenant"]
    inactive = [record for record in records if record["evidence"].get("days_since_activity", 0) > 90]
    labels = Counter(record["evidence"].get("sensitivity_label", "Not collected") for record in records if record["level"] == "item")
    unused = [record for record in records if record["evidence"].get("schedule_enabled") is True and record["evidence"].get("days_since_downstream_read", 0) >= 60]
    for column, title, value in zip(st.columns(4), ["Tenant policy findings", "Inactive >90 days", "Labels observed", "Unread schedules >60 days"],
                                     [sum(finding["level"] == "tenant" for finding in governance), len(inactive), sum(count for label, count in labels.items() if label != "Not collected"), len(unused)]):
        with column.container(border=True, key="pilot_card_" + title.split()[0]):
            st.metric(title, value)
    left, right = st.columns(2)
    with left:
        st.subheader("Tenant security drift")
        policy_rows = []
        for record in tenants:
            for field, label in [("publish_to_web_unrestricted", "Publish to web"), ("trials_unrestricted", "Self-service trials"), ("purview_enforced", "Purview enforcement")]:
                value = record["evidence"].get(field)
                drift = value is not None and (not value if field == "purview_enforced" else value)
                policy_rows.append({"Setting": label, "Observed": "Not collected" if value is None else str(value), "State": "Drift" if drift else "Not assessed" if value is None else "Baseline"})
        st.dataframe(pd.DataFrame(policy_rows), hide_index=True, width="stretch")
        st.subheader("Inactive and orphaned assets")
        action_table([{"Asset": record["name"], "Workspace": _workspace_name(principal, record.get("workspace_id")), "Inactive days": record["evidence"]["days_since_activity"]} for record in inactive], [record["id"] for record in inactive], "pilot_inactive")
        st.caption("Inactivity evidence is incomplete for resources without activity telemetry.")
    with right:
        st.subheader("Purview sensitivity labels")
        if labels:
            chart = alt.Chart(pd.DataFrame([{"Label": label, "Assets": count} for label, count in labels.items()])).mark_arc(innerRadius=48).encode(theta="Assets:Q", color=alt.Color("Label:N", scale=alt.Scale(range=["#2563eb", "#159b85", "#d5a229", "#9ba6b7"])), tooltip=["Label", "Assets"]).properties(height=180)
            st.altair_chart(chart, width="stretch")
        else:
            st.caption("No item labeling evidence.")
        st.subheader("Unused scheduled compute")
        action_table([{"Workload": record["name"], "Days without reads": record["evidence"]["days_since_downstream_read"], "Schedule": "Enabled"} for record in unused], [record["id"] for record in unused], "pilot_schedules", action="Review pause")
        st.caption("Pause schedule requires verified dependency and scheduler permissions.")


def _audit_exports(findings, principal):
    exported = pd.DataFrame([{key: finding[key] for key in ["severity", "title", "asset", "level", "category", "recommendation"]} for finding in findings])
    safe_csv = exported.map(lambda value: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value).to_csv(index=False)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "FABRIC PILOT | Architecture audit", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 6, ("DEMO SNAPSHOT" if principal.demo else "AUTHORIZED SCOPE") + f" | {len(findings)} filtered findings", new_x="LMARGIN", new_y="NEXT")
    for finding in findings:
        text = f"{finding['severity']} | {finding['title']}\n{finding['level']} / {finding['asset']}\n{finding['recommendation']}\n"
        pdf.multi_cell(0, 6, text.encode("latin-1", "replace").decode("latin-1"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    with st.container(horizontal=True):
        st.download_button("Export CSV", safe_csv, file_name="fabric-pilot-audit.csv", mime="text/csv", icon=":material/download:")
        st.download_button("Export PDF", bytes(pdf.output()), file_name="fabric-pilot-audit.pdf", mime="application/pdf", icon=":material/picture_as_pdf:")


def _model_catalog(principal, records):
    models = [record for record in records if record["kind"] == "SemanticModel"]
    rows = []
    for model in models:
        evidence = model["evidence"]
        events, queries = evidence.get("directquery_events"), evidence.get("query_count")
        rows.append({"Model": model["name"], "Workspace": _workspace_name(principal, model["workspace_id"]),
                     "Storage mode": "Direct Lake" if evidence.get("directlake") else evidence.get("storage_mode", "Not collected"),
                     "Memory / limit": f"{evidence.get('memory_gb', '?')} / {evidence.get('memory_limit_gb', '?')} GB",
                     "Fallback rate": events / queries if events is not None and queries else None,
                     "DQ events": events, "Cache refresh": evidence.get("last_cache_refresh", "Not collected")})
    action_table(rows, [model["id"] for model in models], "pilot_models", action="Deep dive",
                 column_config={"Fallback rate": st.column_config.NumberColumn(format="percent", width="small")})


def _backlog_type(finding, records):
    record = next(record for record in records if record["id"] == finding["record_id"])
    if record["kind"] in ("Notebook", "SemanticModel", "DataPipeline"):
        return "Code / draft PR"
    return "Maintenance" if record["kind"] == "Lakehouse" else "Owner review"


def simulation_matches(playbook_id, record, settings):
    evidence = record.get("evidence", {})
    error = str(evidence.get("error", "")).casefold()
    if playbook_id == "capacity-shield":
        return evidence.get("http_status") == 430 or (evidence.get("smoothed_utilization", 0) * 100 >= settings.get("utilization", 90)
            and evidence.get("sustained_seconds", 0) >= settings.get("minutes", 5) * 60)
    if playbook_id in ("zombie-reaper", "freshness"):
        baseline = evidence.get("baseline_seconds", 0)
        return bool(baseline and evidence.get("baseline_days", 0) >= 30 and evidence.get("status") == "InProgress"
                    and evidence.get("elapsed_seconds", 0) > baseline * settings.get("runtime_percent", 300 if playbook_id == "zombie-reaper" else 140) / 100)
    if playbook_id == "spark-oom":
        return "outofmemory" in error or "driver memory" in error
    if playbook_id == "delta-lock":
        return "concurrentappendexception" in error
    return bool(playbook_id == "directlake-recovery" and evidence.get("directlake") and evidence.get("directquery_events", 0) > 0)


def _playbook_policies(principal, records):
    settings_by_id = st.session_state.setdefault("pilot_policy_settings", {})
    st.caption("Session simulation policies / production execution disconnected")
    for playbook_id, playbook in PLAYBOOKS.items():
        settings = settings_by_id.get(playbook_id, {})
        history = [event for event in st.session_state.get("pilot_audit", []) if event["Playbook"] == playbook["name"]]
        with st.container(border=True, key=f"pilot_card_policy_{playbook_id}"):
            heading, metric = st.columns([4, 1])
            heading.subheader(playbook["name"])
            heading.caption(playbook["trigger"])
            metric.metric("Simulations", len(history))
            with st.form(f"policy_form_{playbook_id}", border=False):
                columns = st.columns([3, 1])
                mode = columns[0].segmented_control("Policy mode", ["Disabled", "Simulate only"], default=settings.get("mode", "Disabled"), key=f"policy_mode_{playbook_id}", disabled=not principal.demo) or "Disabled"
                columns[1].caption("Active: unavailable")
                values = {"mode": mode}
                if playbook_id == "capacity-shield":
                    first, second = st.columns(2)
                    values["utilization"] = first.number_input("CU threshold (%)", min_value=50, max_value=100, value=settings.get("utilization", 90), key="policy_cu", disabled=not principal.demo)
                    values["minutes"] = second.number_input("Sustained minutes", min_value=1, max_value=60, value=settings.get("minutes", 5), key="policy_minutes", disabled=not principal.demo)
                elif playbook_id == "delta-lock":
                    values["delays"] = [column.number_input(f"Retry {index + 1} delay (sec)", min_value=1, max_value=3600, value=settings.get("delays", [30, 60, 120])[index], key=f"policy_retry_{index}", disabled=not principal.demo) for index, column in enumerate(st.columns(3))]
                elif playbook_id in ("zombie-reaper", "freshness"):
                    values["runtime_percent"] = st.number_input("Runtime versus baseline (%)", min_value=100, max_value=1000, value=settings.get("runtime_percent", 300 if playbook_id == "zombie-reaper" else 140), key=f"policy_runtime_{playbook_id}", disabled=not principal.demo)
                if st.form_submit_button("Save simulation policy", icon=":material/save:", disabled=not principal.demo):
                    settings_by_id[playbook_id] = values
                    st.session_state.pop("pilot_approved", None)
                    st.rerun()
            matched = sum(simulation_matches(playbook_id, record, settings) for record in records)
            st.caption(f"Saved mode: {settings.get('mode', 'Disabled')} / {matched} matching scoped resources")
            if playbook_id == "delta-lock":
                st.caption("Simulation retry schedule: " + ", ".join(f"{delay}s" for delay in settings.get("delays", [30, 60, 120])))
            if history:
                st.dataframe(pd.DataFrame(history[-3:])[['Time', 'Resource', 'Outcome']], hide_index=True, width="stretch")
            else:
                st.caption("No interventions recorded in this session.")


def _ignore_selected(selected_id):
    ignored = st.session_state.setdefault("pilot_ignored", [])
    if selected_id not in ignored:
        ignored.append(selected_id)
    _close_detail()


def _demo_diff(record):
    if record["kind"] == "Notebook":
        return '- result = source.groupBy("customer_id").count()\n+ spark.conf.set("spark.sql.adaptive.enabled", "true")\n+ result = source.repartition("customer_id").groupBy("customer_id").count()'
    if record["kind"] == "SemanticModel":
        return '- crossFilteringBehavior: bothDirections\n+ crossFilteringBehavior: oneDirection'
    return '- "activities": [{"name": "CopyRows", "type": "Copy"}]\n+ "activities": [{"name": "Ingest", "type": "Copy"}, {"name": "NotifyFailure", "dependsOn": [{"activity": "Ingest", "dependencyConditions": ["Failed"]}]}]'


def render_operations_page(menu, principal, records):
    st.title(menu)
    st.caption("DEMO DATA / synthetic tenant / no external changes" if principal.demo else "LIVE INVENTORY / unavailable telemetry remains not assessed")
    results = assessments(principal, records)
    findings = [result for result in results if result["status"] == "Finding"]
    active = incidents(principal, records)
    if not principal.demo:
        if st.button("Refresh delegated inventory", icon=":material/refresh:", key="pilot_refresh"):
            try:
                with st.spinner("Verifying identity and collecting permitted workspaces..."):
                    refresh_live_session()
                st.rerun()
            except Exception:
                st.error("Permission discovery or collection failed. Check Graph/Fabric consent and the delegated connection.")

    if menu == "Live SRE & Incidents":
        categories = {"All incidents": None, "Throttling": "capacity-shield", "Spark OOM": "spark-oom", "Delta locks": "delta-lock", "Direct Lake": "directlake-recovery", "Zombie jobs": "zombie-reaper", "Freshness": "freshness"}
        counts = Counter(incident["playbook"] for incident in active)
        choice = st.pills("Incident category", list(categories), default="All incidents", key="pilot_incident_category",
                          format_func=lambda name: f"{name} ({len(active) if categories[name] is None else counts[categories[name]]})") or "All incidents"
        with st.container(horizontal=True):
            priority = st.selectbox("Severity", ["All severities", "Critical", "High", "Medium", "Low"], key="pilot_incident_severity")
            visibility = st.selectbox("Status", ["Open", "Ignored in session", "All"], key="pilot_incident_status")
        ignored = st.session_state.get("pilot_ignored", [])
        subset = [incident for incident in active if (categories[choice] is None or incident["playbook"] == categories[choice])
                  and (priority == "All severities" or incident["severity"] == priority)
                  and (visibility == "All" or (incident["id"] in ignored) == (visibility == "Ignored in session"))]
        action_table([{"Severity": incident["severity"], "Incident type": incident["title"], "Asset": incident["asset"],
                       "Workspace": _workspace_name(principal, incident.get("workspace_id")),
                       "Runtime / baseline": f"{incident['elapsed_seconds'] / 60:.0f} / {incident['baseline_seconds'] / 60:.0f} min" if incident.get("baseline_seconds") else "Not collected",
                       "Trigger": PLAYBOOKS[incident["playbook"]]["trigger"] if incident.get("playbook") in PLAYBOOKS else "Failure event",
                       "Status": "Ignored" if incident["id"] in ignored else "Open"} for incident in subset], [incident["id"] for incident in subset], "pilot_incidents", action="Triage")
        st.subheader("SLA and runtime risk")
        _runtime_chart(subset)
    elif menu == "Architecture Audits":
        subset = _filtered_findings(findings, "pilot_architecture")
        st.caption(f"{len(subset)} findings / {sum(result['status'] == 'Not assessed' for result in results)} checks without evidence" + (" / assessed " + st.session_state.pilot_audit_time if st.session_state.get("pilot_audit_time") else ""))
        action_table([{"Severity": finding["severity"], "Audit rule / finding": finding["title"],
                       "Scope / asset": finding["level"].title() + " / " + finding["asset"], "Pillar": finding["category"],
                       "Estimated impact": "Not quantified"} for finding in subset], [finding["id"] for finding in subset], "pilot_findings", action="Inspect & remediate")
        _audit_exports(subset, principal)
        with st.expander("All check results"):
            st.dataframe(pd.DataFrame(results).drop(columns=["evidence"], errors="ignore"), hide_index=True, width="stretch")
    elif menu == "Direct Lake Watchdog":
        st.subheader("Semantic model catalog")
        _model_catalog(principal, records)
        st.subheader("Memory, fallback and modeling findings")
        _finding_list([finding for finding in findings if finding["category"] == "Direct Lake"])
    elif menu == "Governance & Sprawl Control":
        _governance(principal, records, findings)
    elif menu == "Remediation Backlog":
        subset = sorted(_filtered_findings(findings, "pilot_backlog"), key=lambda finding: (finding["severity"] != "High", finding["title"]))
        ignored = st.session_state.get("pilot_ignored", [])
        action_table([{"Severity": finding["severity"], "Recommendation": finding["title"], "Resource": finding["asset"],
                       "Projected benefit": "Not quantified", "Autofix type": _backlog_type(finding, records),
                       "Status": "Ignored in session" if finding["id"] in ignored else "Needs review"} for finding in subset],
                     [finding["id"] for finding in subset], "pilot_backlog", action="Review remediation")
        _audit_exports(subset, principal)
    elif menu == "Self-Healing Playbooks":
        _playbook_policies(principal, records)
    elif menu == "Capacity & Cost Optimization":
        st.info("Capacity telemetry is not connected. Inventory alone cannot establish CU usage, smoothing or billing costs.")
    render_context_drawer(principal, records, results, active)


def render_context_drawer(principal, records, results, active):
    selected_id = st.session_state.get("pilot_detail")
    selected = next((entry for entry in results + active + records if entry["id"] == selected_id), None)
    if selected is None:
        return
    record = next((record for record in records if record["id"] == selected.get("record_id", selected["id"])), None)
    if record is None:
        return
    playbook_id = selected.get("playbook")
    playbook = PLAYBOOKS.get(playbook_id)
    path = _workspace_name(principal, record.get("workspace_id")) + " / " + record["kind"] + " / " + record["name"]
    source = record.get("source", "unknown") + " / " + record.get("observed_at", "")
    with inspection_panel(selected.get("title", record["name"]), path, selected.get("severity", "Inspection"), source,
                          "pilot_close_detail", _close_detail) as (root_cause, telemetry):
        with root_cause:
            st.subheader("Root-cause assessment")
            st.write(selected.get("recommendation", "Correlate the observed signal with execution and capacity telemetry before applying changes."))
            related = [finding for finding in results if finding["record_id"] == record["id"] and finding["status"] == "Finding"]
            if record["kind"] == "SemanticModel":
                for violation in record["evidence"].get("bpa_violations", []):
                    st.write("- " + violation)
                cardinality = record["evidence"].get("column_cardinality")
                if cardinality:
                    st.dataframe(pd.DataFrame(cardinality), hide_index=True, width="stretch")
                else:
                    st.caption("Column cardinality and DAX query traces not collected.")
            if selected.get("title") is None:
                for finding in related:
                    st.write("**" + finding["title"] + "**")
                    st.write(finding["recommendation"])
            st.link_button("Microsoft Fabric reference", "https://learn.microsoft.com/fabric/", icon=":material/open_in_new:")
            if st.button("Generate AI diagnosis", icon=":material/psychology:", key="pilot_diagnose", disabled=not ai_enabled(principal)):
                try:
                    with st.spinner("Analyzing redacted evidence..."):
                        st.session_state.pilot_diagnosis = {"record_id": record["id"], "result": diagnose(principal, record)}
                except Exception:
                    st.error("AI diagnosis unavailable. No action was executed.")
            diagnosis = st.session_state.get("pilot_diagnosis", {})
            if diagnosis.get("record_id") == record["id"]:
                st.json(diagnosis["result"])
            else:
                st.caption("AI provider: not connected" if not ai_enabled(principal) else "AI diagnosis not requested")
            if playbook:
                st.subheader(playbook["name"])
                for number, step in enumerate(playbook["steps"], start=1):
                    st.write(f"{number}. {step}")
            if st.session_state.get("pilot_preview") == record["id"]:
                st.subheader("Illustrative demo PR preview")
                st.caption("Not repository source / not validated for deployment")
                st.code(_demo_diff(record), language="diff", wrap_lines=True)
                st.button("Publish draft PR", icon=":material/merge:", disabled=True, help="Verified repository, source files and approval are required.")
            if record["kind"] in ("Lakehouse", "SemanticModel"):
                st.button("OPTIMIZE & reload cache", icon=":material/refresh:", disabled=True, help="Requires verified fragmentation evidence, maintenance approval and a connected execution adapter.", key="pilot_optimize")
            if record["evidence"].get("schedule_enabled"):
                st.button("Pause schedule", icon=":material/pause:", disabled=True, help="Dependency verification and schedule-write adapter are not connected.")
        with telemetry:
            st.subheader("Evidence & stacktrace")
            st.code(redact_text(json.dumps(record.get("evidence", {}), indent=2)), language="json", wrap_lines=True)
            history = record["evidence"].get("duration_history")
            if history:
                st.line_chart(pd.DataFrame({"Run": list(range(1, len(history) + 1)), "Duration (seconds)": history}), x="Run", y="Duration (seconds)", height=160)
            else:
                st.caption("Historical durations not collected.")
    resource = Resource(principal.tenant_id, record.get("workspace_id", ""), record.get("capacity_id", ""), record["id"])
    settings = st.session_state.get("pilot_policy_settings", {}).get(playbook_id, {})
    action_allowed = bool(playbook) and allowed(principal, Action(playbook["action"]), resource)
    enabled = principal.demo and action_allowed and settings.get("mode") == "Simulate only" and simulation_matches(playbook_id, record, settings)
    with inspection_footer():
        if playbook:
            approved = st.checkbox("Approve this simulation", key="pilot_approved", disabled=not enabled)
            with st.container(horizontal=True):
                st.button("Ignore", icon=":material/visibility_off:", on_click=_ignore_selected, args=(selected_id,), key="pilot_ignore")
                if st.button("Execute healing simulation", icon=":material/auto_fix_high:", key="pilot_execute", type="primary", disabled=not (enabled and approved)):
                    st.session_state.setdefault("pilot_audit", []).append({"Time": datetime.now(timezone.utc).isoformat(),
                        "Tenant": principal.tenant_id, "Actor": principal.object_id, "Playbook": playbook["name"],
                        "Resource": record["name"], "Outcome": "Simulated; no external changes", "Settings": settings.copy()})
                    st.toast("Simulation recorded. No external resources changed.")
            st.caption("Session simulation only / live execution disconnected")
        else:
            with st.container(horizontal=True):
                st.button("Dismiss", icon=":material/close:", on_click=_close_detail, key="pilot_dismiss")
                code_asset = record["kind"] in ("Notebook", "SemanticModel", "DataPipeline")
                can_preview = principal.demo and code_asset and allowed(principal, Action.RUN, resource)
                if st.button("Generate PR preview", icon=":material/merge:", type="primary", key="pilot_preview_pr", disabled=not can_preview,
                             help="Illustrative demo diff. Repository source and live publishing are not connected."):
                    st.session_state.pilot_preview = record["id"]
                    st.rerun()
            st.caption("No external changes / repository publishing disconnected")