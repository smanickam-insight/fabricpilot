"""Per-session, per-identity UI data. Never shares user credentials or telemetry."""

from datetime import datetime, timedelta, timezone
import os
from uuid import UUID

import requests
import streamlit as st

from services.access import AccessDenied, Principal, visible_records
from services.collector import collect_inventory
from services.demo_estate import demo_principal, demo_records
from services.identity import discover_principal


def hosted_identity_gate():
    if os.getenv("PILOT_HOSTED", "false").lower() != "true":
        return
    if not st.user.is_logged_in:
        st.title("FABRIC PILOT")
        st.button("Sign in with Microsoft", icon=":material/login:", on_click=st.login, args=("microsoft",))
        st.stop()
    allowed_tenants = {tenant.strip() for tenant in os.getenv("PILOT_ALLOWED_TENANTS", "").split(",")}
    claims = st.user.to_dict()
    tenant, object_id = claims.get("tid"), claims.get("oid")
    if (tenant not in allowed_tenants or not object_id
            or claims.get("exp", 0) <= datetime.now(timezone.utc).timestamp()):
        st.error("This identity is expired or its tenant is not onboarded.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    previous = st.session_state.get("platform_oidc_identity")
    if previous != (tenant, object_id):
        from services.auth import sign_out
        sign_out()
    st.session_state.platform_oidc_identity = (tenant, object_id)
    st.session_state.authenticated = True
    st.session_state.account_info = claims.get("name", object_id)


def principal_for_session():
    if not st.session_state.get("authenticated"):
        return demo_principal()
    principal = st.session_state.get("platform_principal")
    if principal and principal.expires_at and principal.expires_at > datetime.now(timezone.utc):
        return principal
    tenant, object_id = st.session_state.get("platform_oidc_identity", ("unverified", "unverified"))
    return Principal(tenant, object_id, st.session_state.get("account_info", "Signed-in user"),
                     expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))


def records_for_session(principal):
    owner = (principal.tenant_id, principal.object_id, principal.demo)
    if st.session_state.get("platform_owner") != owner:
        for key in list(st.session_state):
            if key.startswith(("platform_", "pilot_")) and key != "platform_principal":
                del st.session_state[key]
        st.session_state.platform_owner = owner
        st.session_state.pop("detail_topic", None)
    records = demo_records() if principal.demo else st.session_state.get("platform_records", [])
    records = visible_records(principal, records)
    workspace_ids = st.session_state.get("platform_workspaces", [grant.workspace_id for grant in principal.workspaces])
    capacity_ids = st.session_state.get("platform_capacities", list(principal.capacity_ids))
    hours = {"Last 1h": 1, "Last 24h": 24, "Last 7d": 168, "Last 30d": 720}
    anchor = datetime.now(timezone.utc)
    if principal.demo and records:
        anchor = max(datetime.fromisoformat(record["observed_at"]) for record in records)
    cutoff = anchor - timedelta(hours=hours.get(st.session_state.get("platform_timeframe"), 24))
    return [record for record in records if
            (not record.get("workspace_id") or record["workspace_id"] in workspace_ids)
            and (st.session_state.get("pilot_capacity_select", "all") == "all" or not record.get("capacity_id") or record["capacity_id"] in capacity_ids)
            and (not record.get("observed_at") or cutoff <= datetime.fromisoformat(record["observed_at"]) <= anchor)]


def refresh_live_session():
    credential = st.session_state.get("credential")
    if credential is None:
        raise AccessDenied("A delegated Fabric connection is required.")
    st.session_state.pop("platform_principal", None)
    st.session_state.pop("platform_records", None)
    st.session_state.pop("pilot_detail", None)
    st.session_state.pop("pilot_diagnosis", None)
    token = credential.get_token("https://graph.microsoft.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}
    user_response = requests.get("https://graph.microsoft.com/v1.0/me?$select=id,displayName", headers=headers, timeout=20, allow_redirects=False)
    organization_response = requests.get("https://graph.microsoft.com/v1.0/organization?$select=id", headers=headers, timeout=20, allow_redirects=False)
    user_response.raise_for_status()
    organization_response.raise_for_status()
    user = user_response.json()
    organizations = organization_response.json().get("value", [])
    if len(organizations) != 1:
        raise AccessDenied("The active directory tenant could not be verified.")
    tenant = str(UUID(organizations[0]["id"]))
    object_id = str(UUID(user["id"]))
    if st.session_state.get("platform_oidc_identity") not in (None, (tenant, object_id)):
        raise AccessDenied("The delegated connection does not match the signed-in OIDC identity.")
    claims = {"tid": tenant, "oid": object_id, "name": user.get("displayName", object_id), "exp": token.expires_on}
    principal = discover_principal(claims, credential)
    records = collect_inventory(principal, credential)
    st.session_state.platform_principal = principal
    st.session_state.platform_owner = (tenant, object_id, False)
    st.session_state.platform_records = records
    st.session_state.platform_workspaces = [grant.workspace_id for grant in principal.workspaces]
    st.session_state.platform_capacities = list(principal.capacity_ids)


def menu_visible(principal, menu):
    if menu == "Governance & Sprawl Control":
        return principal.tenant_admin
    if menu == "Capacity & Cost Optimization":
        return principal.tenant_admin or bool(principal.capacity_ids)
    return True