"""
ADF + Fabric Monitor — unified local Streamlit app.

Screens:
  * 🏠 Home                         (interactive overview + navigation)
  * 📊 Dashboard / Error Analytics  (Log Analytics failure analytics + remediation plan)
  * 🧩 Pipeline Activities          (live ADF REST API — activity-level run details)
  * 🏭 Fabric Monitoring            (live Fabric REST API — workspaces, jobs, capacities)
  * 🔐 Authentication               (one shared Entra ID sign-in)

Operations navigation lives in the sidebar. Home cards can jump between
monitoring views using the existing _pending_tab session state contract.

Run with:  streamlit run app.py
(.streamlit/config.toml pins the bind address to 127.0.0.1 and disables telemetry.)
"""

from pathlib import Path

import streamlit as st
from streamlit.components.v2 import component

from components.authentication import render_authentication_section
from components.dashboard import render_dashboard_section
from components.pipeline_activities import render_pipeline_activities_section
from components.fabric_monitor import render_fabric_monitor_section
from components.capacity import render_capacity_section
from components.operations import render_command_center, render_operations_page, render_scope_filter, _select_detail
from services.assessment import incidents
from services.platform_session import principal_for_session, records_for_session, menu_visible, hosted_identity_gate

st.set_page_config(
    page_title="FABRIC PILOT",
    page_icon=":material/hub:",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    "<style>" + (Path(__file__).parent / "assets" / "pilot.css").read_text(encoding="utf-8") + "</style>",
    unsafe_allow_html=True,
)

KEYBOARD = component("pilot_keyboard", html="<span></span>", js="""
export default function ({setTriggerValue}) {
    const listener = (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault(); setTriggerValue('command', 'search');
        } else if (event.key === 'Escape') {
            setTriggerValue('command', 'close');
        }
    };
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
}
""")

TABS = [
    "🏠 Home",
    "📊 Dashboard",
    "🧩 Pipeline Activities",
    "🏭 Fabric Monitoring",
    "🔐 Authentication",
]

SIDEBAR_MENU = {
    "Command Center / Overview": "Unified architectural scorecards and live platform health.",
    "Live SRE & Incidents": "Event streams, runtime failures, SLA breach predictions, and active self-healing playbooks.",
    "Capacity & Cost Optimization": "CU consumption, throttling, background compute smoothing, and F-SKU right-sizing.",
    "Architecture Audits": "Scheduled assessments for small files, inefficient DAX, and legacy data movement.",
    "Direct Lake Watchdog": "Semantic model monitoring for silent Direct Lake to DirectQuery fallbacks.",
    "Governance & Sprawl Control": "Tenant drift, workspace organization, Purview labels, and unused workloads.",
    "Remediation Backlog": "Prioritized refactoring recommendations and generated pull requests.",
    "Self-Healing Playbooks": "Configuration for throttling, Spark OOM, and Delta file lock automation.",
}

MENU_ICONS = [
    "space_dashboard", "health_and_safety", "monitoring", "fact_check",
    "visibility", "admin_panel_settings", "assignment", "auto_fix_high",
]


def _navigate(menu, tab=None):
    st.session_state.sidebar_menu = menu
    st.session_state.pop("pilot_tool", None)
    st.session_state.pop("detail_topic", None)
    st.session_state.pop("pilot_detail", None)
    if tab is not None:
        st.session_state.active_tab = tab


def _modal(name=None):
    st.session_state.pilot_modal = name
    if name:
        st.session_state.pop("pilot_detail", None)
        st.session_state.pop("detail_topic", None)


def _keyboard_command():
    command = st.session_state.pilot_keyboard.command
    if command == "search":
        _modal("search")
    elif command == "close":
        _modal()
        st.session_state.pop("pilot_detail", None)
        st.session_state.pop("detail_topic", None)


@st.dialog("Account & connections", width="medium", on_dismiss=_modal)
def _account_dialog():
    render_authentication_section()
    st.divider()
    st.subheader("Connected monitoring tools")
    for label in TABS[1:4]:
        if st.button(label, key=f"legacy_{label}", width="stretch"):
            st.session_state.pilot_tool = label
            _modal()
            st.rerun()


@st.dialog("Search Fabric Pilot", width="medium", on_dismiss=_modal)
def _search_dialog(principal, records):
    query = st.text_input("Search assets or operations", placeholder="Search assets or operations...", key="pilot_query").strip().casefold()
    for menu in SIDEBAR_MENU:
        if menu_visible(principal, menu) and query in menu.casefold():
            if st.button(menu, key=f"search_{menu}", icon=":material/arrow_forward:", width="stretch"):
                _navigate(menu)
                _modal()
                st.session_state.pop("pilot_tool", None)
                st.rerun()
    matches = [record for record in records if query in (record["name"] + " " + record["kind"]).casefold()]
    if not matches and query:
        st.caption("No matching assets in the current authorized scope.")
    for record in matches[:20]:
        if st.button(record["name"] + " / " + record["kind"], key=f"search_{record['id']}", icon=":material/database:", width="stretch"):
            _navigate("Architecture Audits")
            _select_detail(record["id"])
            _modal()
            st.session_state.pop("pilot_tool", None)
            st.rerun()


def _topbar(principal, active_incidents):
    with st.container(key="topbar", horizontal=True, vertical_alignment="center", gap="small"):
        with st.container(width="stretch"):
            st.html('''<div class="pilot-brand">
                <span class="pilot-mark" aria-hidden="true"><span></span><span></span><span></span><span></span></span>
                <strong>FABRIC PILOT</strong><small>Platform operations</small></div>''')
        with st.container(key="global_search", width="content"):
            st.button("Search", icon=":material/search:", help="Search assets and operations (Ctrl+K)", on_click=_modal, args=("search",))
        with st.container(key="notifications", width="content"):
            read_ids = st.session_state.get("pilot_read_incidents", [])
            unread = sum(incident["id"] not in read_ids for incident in active_incidents)
            st.markdown(f'<style>.st-key-unread [data-testid="stPopoverButton"]::after {{ content: "{unread}"; }}</style>', unsafe_allow_html=True)
            with st.container(key="read" if not unread else "unread", width="content"):
                with st.popover("", icon=":material/notifications:", help="Notifications", width="content"):
                    st.subheader("Notifications")
                    st.caption("Demo incidents" if principal.demo else "Scoped incident signals")
                    for incident in active_incidents:
                        st.write(incident["title"] + " / " + incident["asset"])
                    if not active_incidents:
                        st.caption("No incidents in the available telemetry.")
                    st.button("Mark all as read", icon=":material/done_all:", on_click=lambda: st.session_state.update(pilot_read_incidents=[incident["id"] for incident in active_incidents]))
        with st.container(key="role_badge", width="content"):
            st.caption("Demo admin" if principal.demo else " / ".join(principal.role_labels))
        initials = "".join(part[0] for part in principal.display_name.split()[:2]).upper() or "GU"
        with st.container(key="profile", width="content"):
            with st.popover(initials, help="User profile and login", width="content"):
                st.write(st.session_state.get("account_info", "Guest session"))
                st.caption("Microsoft Entra ID" if st.session_state.get("authenticated") else "Not signed in")
                st.caption(("Demo roles: " if principal.demo else "Verified roles: ") + ", ".join(principal.role_labels))
                st.button(
                    "Manage account" if st.session_state.get("authenticated") else "Sign in",
                    icon=":material/login:", on_click=_modal,
                    args=("account",), key="profile_login",
                )


def _bind_address_warning():
    try:
        from streamlit import config
        address = config.get_option("server.address")
    except Exception:  # noqa: BLE001
        return
    if address not in ("localhost", "127.0.0.1", "::1"):
        st.error(
            f"⚠️ **This app is bound to `{address or '0.0.0.0 (all interfaces)'}`** — it is "
            "reachable from your network, and visitors could obtain an Azure token as you. "
            "Stop it and relaunch with:\n\n"
            "`streamlit run app.py --server.address=127.0.0.1`",
            icon="🚨",
        )


def _sidebar(principal):
    with st.sidebar:
        st.markdown("### Operations")
        selected_menu = st.session_state.setdefault("sidebar_menu", "Command Center / Overview")
        if not menu_visible(principal, selected_menu):
            selected_menu = st.session_state.sidebar_menu = "Command Center / Overview"
        for menu, icon in zip(SIDEBAR_MENU, MENU_ICONS):
            if not menu_visible(principal, menu):
                continue
            st.button(
                menu, icon=f":material/{icon}:", width="stretch", wrap=True,
                type="primary" if selected_menu == menu else "secondary",
                on_click=_navigate, args=(menu,), key=f"nav_{icon}",
            )
        with st.container(key="session"):
            st.markdown("### Session")
            st.markdown(":material/account_circle: " + st.session_state.get("account_info", "Guest workspace"))
            st.caption("Signed in with Microsoft Entra ID" if st.session_state.get("authenticated") else "Not signed in")
            st.button("Account & connections", icon=":material/settings:", on_click=_modal,
                      args=("account",), key="account_settings")
            st.caption("Demo tenant / no access token" if principal.demo else "Tenant: " + principal.tenant_id)
            st.caption("ADF · Fabric · Log Analytics")
            st.caption("Localhost only · Telemetry off\n\nSensitive error text redacted")
    return st.session_state.sidebar_menu


def main():
    hosted_identity_gate()
    principal = principal_for_session()
    records = records_for_session(principal)
    KEYBOARD(key="pilot_keyboard", height=0, on_command_change=_keyboard_command)
    _topbar(principal, incidents(principal, records))
    render_scope_filter(principal)
    _bind_address_warning()
    selected_menu = _sidebar(principal)

    tool = st.session_state.get("pilot_tool")
    if tool:
        st.button("Back to operations", icon=":material/arrow_back:", on_click=lambda: st.session_state.pop("pilot_tool", None))
        {TABS[1]: render_dashboard_section, TABS[2]: render_pipeline_activities_section,
         TABS[3]: render_fabric_monitor_section}[tool]()
    elif selected_menu == "Capacity & Cost Optimization" and principal.demo and any(record["kind"] == "Capacity" for record in records):
        render_capacity_section()
    elif selected_menu != "Command Center / Overview":
        render_operations_page(selected_menu, principal, records)
    else:
        st.title("Command Center")
        render_command_center(principal, records)
    if st.session_state.get("pilot_modal") == "account":
        _account_dialog()
    elif st.session_state.get("pilot_modal") == "search":
        _search_dialog(principal, records)


if __name__ == "__main__":
    main()
