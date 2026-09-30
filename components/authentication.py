"""Authentication tab: interactive Entra ID sign-in + scope selection.

One sign-in serves all four data planes:
  * Azure Resource Manager  (subscriptions, factories, workspaces)
  * Log Analytics Query API (KQL)
  * Azure Data Factory REST API
  * Microsoft Fabric REST API
"""

import os

import streamlit as st

from services.auth import DEFAULT_CLIENT_ID, build_credential, sign_out
from services.platform_session import refresh_live_session


def render_authentication_section():
    st.header("🔐 Authentication")
    if os.getenv("PILOT_HOSTED", "false").lower() == "true":
        st.write(st.session_state.get("account_info", "Microsoft Entra ID session"))
        st.caption("OIDC identity verified. Delegated API access requires an access token for the Fabric Pilot API and OBO consent.")
        if st.button("Sign out of Fabric Pilot", icon=":material/logout:"):
            sign_out()
            st.logout()
        return
    st.caption(
        "Sign in with Microsoft Entra ID (interactive browser). The app acts as **you** — "
        "it only sees subscriptions, factories, Fabric workspaces and runs your account "
        "is allowed to read."
    )

    if st.session_state.get("authenticated"):
        st.success(f"Signed in as **{st.session_state.get('account_info', 'unknown account')}**")
        st.info(f"Tenant: `{st.session_state.get('tenant_id') or 'organizations (multi-tenant)'}`")

        with st.expander("🎫 Token scopes used by this session", expanded=False):
            st.markdown(
                "| Data plane | Scope |\n"
                "|---|---|\n"
                "| Azure Resource Manager / ADF | `https://management.azure.com/.default` |\n"
                "| Log Analytics query | `https://api.loganalytics.io/.default` |\n"
                "| Microsoft Fabric | `https://api.fabric.microsoft.com/.default` |\n"
                "| Email escalation (optional) | `https://graph.microsoft.com/.default` |"
            )
            st.caption(
                "Each scope is acquired silently from the same cached sign-in — you are "
                "prompted only once."
            )

        if st.session_state.get("persist_cache"):
            st.caption("🔒 Token cache persisted (OS-encrypted). Sign out to purge it from disk.")
        else:
            st.caption("🔒 Token held in memory only — it is discarded when the app stops.")

        if st.button("Refresh Fabric permissions & inventory", icon=":material/refresh:", key="platform_refresh_access"):
            try:
                with st.spinner("Verifying tenant, user and delegated workspace permissions..."):
                    refresh_live_session()
                st.success("Scoped workspace inventory refreshed.")
                st.rerun()
            except Exception:
                st.error("Unable to verify Graph/Fabric permissions. Administrative and execution access remain unavailable.")

        if st.button("🚪 Sign out"):
            removed = sign_out()
            st.session_state["_signout_note"] = (
                f"Signed out. Purged {len(removed)} cache file(s) from disk."
                if removed else "Signed out. No persisted cache file was present."
            )
            st.rerun()
        return

    if note := st.session_state.pop("_signout_note", None):
        st.info(note)

    with st.form("adf_auth_form"):
        tenant_id = st.text_input(
            "Tenant ID (optional)",
            value="",
            help="Leave blank to sign in against any tenant your account belongs to. "
                 "Provide a tenant GUID or domain to target a specific tenant.",
        )
        client_id = st.text_input(
            "Client ID (optional)",
            value=DEFAULT_CLIENT_ID,
            help="Defaults to the Azure CLI public client. Sign-in logs will attribute "
                 "activity to 'Azure CLI'. Register your own public client app "
                 "(redirect URI http://localhost) for shared or long-term use, and grant it "
                 "Fabric delegated permissions (Workspace.Read.All, Item.Read.All).",
        )
        persist_cache = st.checkbox(
            "Remember me on this machine",
            value=False,
            help="Stores an OS-encrypted refresh token so you are not prompted next launch. "
                 "If your OS has no working keyring, sign-in will fail rather than write "
                 "the token in plaintext. Leave off to keep the token in memory only.",
        )
        submitted = st.form_submit_button("🔑 Sign in with Microsoft", type="primary")

    if not submitted:
        return

    with st.spinner("A browser window will open — complete sign-in there..."):
        try:
            cred = build_credential(
                tenant_id.strip() or None,
                client_id.strip() or None,
                persist_cache=persist_cache,
            )
            token = cred.get_token("https://management.azure.com/.default")
            st.session_state.credential = cred
            st.session_state.authenticated = True
            st.session_state.tenant_id = tenant_id.strip()
            st.session_state.persist_cache = persist_cache
            st.session_state.account_info = _decode_upn(token.token)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Authentication failed: {exc}")
            if persist_cache:
                st.info(
                    "If this failed with a token-cache/encryption error, no OS keyring is "
                    "available. Uncheck **Remember me on this machine** and sign in again — "
                    "the token will be kept in memory only."
                )
            return

    st.rerun()


def _decode_upn(jwt: str) -> str:
    """
    Best-effort extraction of upn/email from the access token payload.

    Display only — the signature is deliberately NOT verified here, and this
    value must never be used for an authorization decision.
    """
    import base64
    import json
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("upn") or claims.get("unique_name") or claims.get("name", "signed-in user")
    except Exception:  # noqa: BLE001
        return "signed-in user"
