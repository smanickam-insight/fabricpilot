"""
🏠 Home — interactive landing screen for the unified ADF + Fabric Monitor.

Navigation between screens is done by setting st.session_state["_pending_tab"]
and calling st.rerun(); app.py reconciles that into the active screen BEFORE the
navigation radio is created (Streamlit forbids mutating a widget-keyed value
after the widget exists).
"""

from __future__ import annotations

import datetime as dt

import streamlit as st


def _go(tab_label: str):
    """Request app.py to switch to a given tab on next run (via _pending_tab)."""
    st.session_state["_pending_tab"] = tab_label
    st.rerun()


def _status_chip(ok: bool, ok_text: str, no_text: str) -> str:
    dot = "🟢" if ok else "⚪"
    return f"{dot} {ok_text if ok else no_text}"


def render_home_section():
    authed = bool(st.session_state.get("authenticated"))
    account = st.session_state.get("account_info", "not signed in")
    has_analysis = st.session_state.get("analysis") is not None
    has_fabric = st.session_state.get("fb_analysis") is not None

    # ---- Hero -------------------------------------------------------------
    st.markdown(
        """
        <div style="
            padding: 26px 30px; border-radius: 16px; margin-bottom: 8px;
            background: linear-gradient(120deg,#0b5cad 0%,#1e88e5 55%,#42a5f5 100%);
            color: #ffffff;">
          <div style="font-size: 1.9rem; font-weight: 700;">📊 ADF + Fabric Monitor</div>
          <div style="font-size: 1.05rem; opacity: .95; margin-top: 4px;">
            One place to watch Azure Data Factory <b>and</b> Microsoft Fabric — failure
            analytics with a built-in remediation plan, live pipeline drill-down, and
            Fabric workspace/job monitoring. Single sign-in, read-only, redacted by default.
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ---- Live status strip ------------------------------------------------
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Sign-in", "Ready" if authed else "Needed",
              help=f"Account: {account}" if authed else "Sign in on the Authentication tab.")
    s2.metric("Dashboard data", "Loaded" if has_analysis else "Not run",
              help="Run an analysis on the Dashboard tab (Demo Data works with no sign-in).")
    s3.metric("Fabric", "Available" if authed else "Locked",
              help="Requires sign-in — reads the live Fabric REST API.")
    s4.metric("Mode", "Live" if authed else "Demo",
              help="Live pulls real Azure/Fabric data; Demo uses a synthetic dataset.")

    st.caption(
        _status_chip(authed, f"Signed in as {account}", "Not signed in — Demo Data still works")
        + "   ·   "
        + _status_chip(has_analysis, "ADF analysis loaded", "No ADF analysis run yet")
        + "   ·   "
        + _status_chip(has_fabric, "Fabric analysis loaded", "No Fabric analysis run yet")
        + f"   ·   🕒 {dt.datetime.now():%a %d %b %Y, %H:%M}"
    )

    st.markdown("---")

    # ---- Navigation cards -------------------------------------------------
    st.markdown("### Where do you want to go?")
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        with st.container(border=True):
            st.markdown("#### 🔐 Authentication")
            st.write(
                "Sign in once with Microsoft Entra ID. The same session powers ADF, "
                "Log Analytics and Fabric."
            )
            st.caption(_status_chip(authed, "Signed in", "Sign-in required"))
            if st.button(
                "Sign out" if authed else "Go to sign-in",
                key="home_go_auth", width='stretch',
            ):
                _go("🔐 Authentication")

    with c2:
        with st.container(border=True):
            st.markdown("#### 📊 Dashboard")
            st.write(
                "ADF failure analytics + a **remediation plan for recurring failures**: "
                "trends, root cause, new-vs-recurring, anomalies, alerts."
            )
            st.caption("🟢 Demo Data works instantly — no sign-in needed")
            if st.button("Open Dashboard", key="home_go_dash",
                         type="primary", width='stretch'):
                _go("📊 Dashboard")

    with c3:
        with st.container(border=True):
            st.markdown("#### 🧩 Pipeline Activities")
            st.write(
                "Pick a Subscription → Data Factory → Pipeline → Run and inspect "
                "activity-level logs, copy metrics and links, live from the ADF REST API."
            )
            st.caption(_status_chip(authed, "Ready", "Sign in to unlock"))
            if st.button("Open Pipeline Activities", key="home_go_pipe",
                         width='stretch', disabled=not authed):
                _go("🧩 Pipeline Activities")
            if not authed:
                st.caption("🔒 Requires sign-in.")

    with c4:
        with st.container(border=True):
            st.markdown("#### 🏭 Fabric Monitoring")
            st.write(
                "Fabric workspaces, data-pipeline & notebook job runs, capacities, and "
                "Fabric error analytics — live from the Fabric REST API."
            )
            st.caption(_status_chip(authed, "Ready", "Sign in to unlock"))
            if st.button("Open Fabric Monitoring", key="home_go_fabric",
                         width='stretch', disabled=not authed):
                _go("🏭 Fabric Monitoring")
            if not authed:
                st.caption("🔒 Requires sign-in.")

    st.markdown("---")

    # ---- Quick start ------------------------------------------------------
    left, right = st.columns([1, 1])

    with left:
        st.markdown("### 🚀 Quick start")
        st.markdown(
            "1. **Sign in** on the *Authentication* tab *(skip for a Demo-only walkthrough)*.\n"
            "2. Open the **Dashboard**, choose *Demo Data* or *Live*, set a lookback, click **Run Analysis**.\n"
            "3. Scroll to the **Remediation / Resolution Plan** to get prioritised fixes for recurring failures.\n"
            "4. Jump to **Pipeline Activities** to drill into a specific failing ADF run.\n"
            "5. Switch to **Fabric Monitoring** for workspace, job-run, capacity and Fabric error analytics."
        )
        b1, b2 = st.columns(2)
        if b1.button("▶️ Try Demo Dashboard", key="home_try_demo", width='stretch'):
            _go("📊 Dashboard")
        if b2.button("🔐 Sign in for Live data", key="home_try_live", width='stretch'):
            _go("🔐 Authentication")

    with right:
        st.markdown("### 🧭 Which view do I need?")
        st.dataframe(
            {
                "Question": [
                    "How many ADF failures today vs yesterday?",
                    "Which errors are new vs recurring?",
                    "What's the fix for a repeat failure?",
                    "Why did *this* ADF run fail, step by step?",
                    "Which Fabric notebooks/pipelines are failing?",
                    "What SKU is my Fabric capacity on?",
                ],
                "Go to": [
                    "📊 Dashboard", "📊 Dashboard", "📊 Dashboard → Remediation Plan",
                    "🧩 Pipeline Activities",
                    "🏭 Fabric Monitoring → Error Analytics",
                    "🏭 Fabric Monitoring → Capacities",
                ],
            },
            hide_index=True,
            width='stretch',
        )

    st.markdown("---")

    # ---- Capability matrix + safety ---------------------------------------
    with st.expander("🔎 Capability matrix", expanded=False):
        st.dataframe(
            {
                "Capability": [
                    "Data source", "Works without sign-in", "Time window",
                    "Anomaly detection", "Root-cause + recommended action",
                    "Remediation plan (recurring)", "Run-level drill-down", "Export",
                ],
                "📊 Dashboard": [
                    "Log Analytics (ADFActivityRun)", "✅ Demo Data", "1–90 days",
                    "✅ z-score", "✅", "✅ MD + CSV", "—", "✅ daily report + plan",
                ],
                "🧩 Pipeline Activities": [
                    "ADF REST API", "❌ sign-in required", "1–45 days",
                    "—", "—", "—", "✅ per activity", "—",
                ],
                "🏭 Fabric Monitoring": [
                    "Fabric REST API", "❌ sign-in required", "job history retention",
                    "✅ z-score", "✅", "✅ MD + CSV", "✅ per job instance", "✅ plan",
                ],
            },
            hide_index=True,
            width='stretch',
        )

    st.info(
        "🔒 **Safe by design:** read-only calls only, bound to `127.0.0.1`, telemetry off, "
        "and secrets (SAS tokens, connection strings, auth headers) are redacted unless you "
        "explicitly toggle raw mode.",
        icon="🛡️",
    )
