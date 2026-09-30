"""
Microsoft Entra ID interactive authentication for the unified ADF + Fabric Monitor.

This single module powers ALL views:
  * Dashboard / Error Analytics -> Log Analytics query API (services/azure_api.py)
  * Pipeline Activities          -> ADF REST API           (services/adf_api.py)
  * Fabric Monitoring            -> Fabric REST API        (services/fabric_api.py)

Security posture:
- Token cache persistence is **opt-in** and, when enabled, requires OS-backed
  encryption (allow_unencrypted_storage=False). If no keyring is available the
  credential fails loudly rather than silently writing a long-lived refresh
  token to a plaintext file.
- sign_out() performs a *real* sign-out: it clears session state **and**
  purges the on-disk MSAL cache.
- Intended to be bound to localhost (see app.py / .streamlit/config.toml).

Token scopes used:
- ARM_SCOPE            -> subscriptions, factories & Log Analytics workspaces (management.azure.com)
- LOG_ANALYTICS_SCOPE  -> execute KQL queries against a workspace (api.loganalytics.io)
- FABRIC_SCOPE         -> Microsoft Fabric REST API (api.fabric.microsoft.com)
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

try:
    from azure.identity import InteractiveBrowserCredential, TokenCachePersistenceOptions
    AZURE_IDENTITY_AVAILABLE = True
except ImportError:
    AZURE_IDENTITY_AVAILABLE = False

ARM_SCOPE = "https://management.azure.com/.default"
ARM_BASE = "https://management.azure.com"

LOG_ANALYTICS_SCOPE = "https://api.loganalytics.io/.default"
LOG_ANALYTICS_BASE = "https://api.loganalytics.io"

# ---- Microsoft Fabric -------------------------------------------------------
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"

CACHE_NAME = "adf_monitor"
DEFAULT_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def build_credential(
    tenant_id: str | None = None,
    client_id: str | None = None,
    persist_cache: bool = False,
) -> "InteractiveBrowserCredential":
    """Create an interactive browser credential (opens a sign-in tab)."""
    if not AZURE_IDENTITY_AVAILABLE:
        raise RuntimeError(
            "azure-identity is not installed in this environment. "
            "Run: pip install azure-identity"
        )

    cache_options = None
    if persist_cache:
        cache_options = TokenCachePersistenceOptions(
            name=CACHE_NAME,
            allow_unencrypted_storage=False,
        )

    kwargs = {}
    if tenant_id:
        kwargs["tenant_id"] = tenant_id

    return InteractiveBrowserCredential(
        client_id=client_id or DEFAULT_CLIENT_ID,
        cache_persistence_options=cache_options,
        **kwargs,
    )


def get_credential():
    """Return the credential stored in session state (or None)."""
    return st.session_state.get("credential")


def _token_for(scope: str) -> str | None:
    cred = get_credential()
    if cred is None:
        return None
    return cred.get_token(scope).token


def get_arm_token() -> str | None:
    """Fetch a bearer token scoped to Azure Resource Manager."""
    return _token_for(ARM_SCOPE)


def get_log_analytics_token() -> str | None:
    """Fetch a bearer token scoped to the Log Analytics query API."""
    return _token_for(LOG_ANALYTICS_SCOPE)


def get_fabric_token() -> str | None:
    """Fetch a bearer token scoped to the Microsoft Fabric REST API."""
    return _token_for(FABRIC_SCOPE)


def _headers(token: str | None) -> dict:
    if not token:
        raise RuntimeError("Not authenticated. Sign in from the Authentication tab.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def arm_headers() -> dict:
    return _headers(get_arm_token())


def log_analytics_headers() -> dict:
    return _headers(get_log_analytics_token())


def fabric_headers() -> dict:
    """Auth headers for the Fabric REST client — same signed-in user, new scope."""
    return _headers(get_fabric_token())


# --------------------------------------------------------------------------- #
# Aliases used by the ADF REST client (services/adf_api.py). The ADF API is
# reached with the same ARM-scoped bearer token, so these forward to the ARM
# helpers above — one credential/session for the whole app.
# --------------------------------------------------------------------------- #
def get_token() -> str | None:
    """Fetch a bearer token for Azure Resource Manager (ADF REST API)."""
    return get_arm_token()


def auth_headers() -> dict:
    """ARM auth headers for the ADF REST client."""
    return arm_headers()


# ---------------------------------------------------------------- sign-out
def _msal_cache_paths() -> list[Path]:
    candidates: list[Path] = []
    home = Path.home()
    candidates.append(home / ".IdentityService" / f"msal_{CACHE_NAME}.bin")
    candidates.append(home / ".cache" / "msal" / f"{CACHE_NAME}.bin")
    if os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / ".IdentityService" / f"msal_{CACHE_NAME}.bin")
    return [p for p in candidates if p.exists()]


def purge_token_cache() -> list[str]:
    removed = []
    for path in _msal_cache_paths():
        try:
            path.unlink()
            removed.append(str(path))
        except Exception:  # noqa: BLE001
            pass
    return removed


def sign_out() -> list[str]:
    """Clear the credential, every cached lookup, and the on-disk token cache."""
    removed = purge_token_cache()
    for key in list(st.session_state.keys()):
        if key.startswith(("la_", "adf_", "pa_", "fb_", "law_", "platform_", "pilot_")) or key in (
            "credential", "authenticated", "account_info", "tenant_id", "persist_cache",
            "analysis", "detail_topic", "notifications_read",
        ):
            st.session_state.pop(key, None)
    return removed


def decode_upn(jwt: str) -> str:
    """Best-effort extraction of upn/email from the access token payload (display only)."""
    import base64
    import json
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("upn") or claims.get("unique_name") or claims.get("name", "signed-in user")
    except Exception:  # noqa: BLE001
        return "signed-in user"
