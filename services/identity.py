"""Validated Entra API identity and explicit delegated/application credentials."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
import requests
from azure.identity import ClientSecretCredential, OnBehalfOfCredential

from services.access import AccessDenied, Principal, WorkspaceGrant


FABRIC_ADMIN_ROLE = "a9ea8996-122f-4c74-9520-8edcd192826c"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_ORIGIN = "https://api.fabric.microsoft.com/v1"


class EntraValidator:
    def __init__(self, audience, allowed_tenants, required_scope="access_as_user"):
        if not audience or not allowed_tenants:
            raise ValueError("An API audience and explicit tenant allowlist are required.")
        self.audience = audience
        self.allowed_tenants = frozenset(str(UUID(tenant)) for tenant in allowed_tenants)
        self.required_scope = required_scope
        self.clients = {}

    def validate(self, assertion):
        try:
            untrusted = jwt.decode(assertion, options={"verify_signature": False})
            tenant = str(UUID(untrusted.get("tid", "")))
            if tenant not in self.allowed_tenants:
                raise AccessDenied("Tenant is not onboarded.")
            issuer = f"https://login.microsoftonline.com/{tenant}/v2.0"
            client = self.clients.setdefault(tenant, jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys", timeout=10))
            key = client.get_signing_key_from_jwt(assertion).key
            claims = jwt.decode(assertion, key, algorithms=["RS256"], audience=self.audience,
                                issuer=issuer, options={"require": ["exp", "iat", "nbf", "tid", "oid", "aud", "iss"]})
            if claims["tid"] != tenant or self.required_scope not in claims.get("scp", "").split():
                raise AccessDenied("A delegated API scope is required.")
            UUID(claims["oid"])
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise AccessDenied("Invalid or expired Entra API token.") from exc


def delegated_credential(assertion, validator, client_id, client_secret):
    claims = validator.validate(assertion)
    credential = OnBehalfOfCredential(tenant_id=claims["tid"], client_id=client_id,
                                      client_secret=client_secret, user_assertion=assertion)
    return claims, credential


def application_credential(tenant_id, client_id, client_secret, allowed_tenants):
    if tenant_id not in allowed_tenants:
        raise AccessDenied("Application credentials are restricted to onboarded tenants.")
    return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)


def _collection(credential, path):
    token = credential.get_token(FABRIC_SCOPE).token
    records = []
    continuation = None
    for _page in range(20):
        response = requests.get(FABRIC_ORIGIN + path,
                                headers={"Authorization": f"Bearer {token}"},
                                params={"continuationToken": continuation} if continuation else None,
                                timeout=20, allow_redirects=False)
        response.raise_for_status()
        body = response.json()
        records.extend(body.get("value", []))
        continuation = body.get("continuationToken")
        if not continuation:
            if body.get("continuationUri"):
                raise AccessDenied("Incomplete permission discovery; continuation token missing.")
            return records
    raise AccessDenied("Permission discovery exceeded the page limit.")


def discover_principal(claims, credential):
    grants = []
    groups = set(claims.get("groups", []))
    ranks = {"Viewer": 0, "Contributor": 1, "Member": 2, "Admin": 3}
    for workspace in _collection(credential, "/workspaces"):
        workspace_id = str(UUID(workspace["id"]))
        role = "Viewer"
        try:
            assignments = _collection(credential, f"/workspaces/{workspace_id}/roleAssignments")
            for assignment in assignments:
                identity = assignment.get("principal", {})
                direct = identity.get("type") == "User" and identity.get("id") == claims["oid"]
                group = identity.get("type") == "Group" and identity.get("id") in groups
                assigned_role = assignment.get("role", "Viewer")
                if (direct or group) and ranks.get(assigned_role, -1) > ranks[role]:
                    role = assigned_role
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 403:
                raise
        grants.append(WorkspaceGrant(workspace_id, workspace.get("displayName", workspace_id),
                                     role, workspace.get("capacityId", "")))
    expires = min(datetime.fromtimestamp(claims["exp"], timezone.utc),
                  datetime.now(timezone.utc) + timedelta(minutes=5))
    return Principal(claims["tid"], claims["oid"], claims.get("name", claims["oid"]),
                     tuple(grants), tenant_admin=FABRIC_ADMIN_ROLE in claims.get("wids", []),
                     expires_at=expires)