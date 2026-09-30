"""Tenant and resource authorization shared by collectors, views, and executors."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AccessDenied(PermissionError):
    pass


class Action(str, Enum):
    READ = "read"
    TENANT_AUDIT = "tenant_audit"
    CAPACITY_READ = "capacity_read"
    SCALE = "scale"
    RUN = "run"
    CANCEL = "cancel"
    PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class WorkspaceGrant:
    workspace_id: str
    name: str
    role: str = "Viewer"
    capacity_id: str = ""


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    object_id: str
    display_name: str
    workspaces: tuple[WorkspaceGrant, ...] = ()
    capacity_ids: frozenset[str] = field(default_factory=frozenset)
    scalable_capacity_ids: frozenset[str] = field(default_factory=frozenset)
    repository_ids: frozenset[str] = field(default_factory=frozenset)
    tenant_admin: bool = False
    demo: bool = False
    expires_at: datetime | None = None

    def grant(self, workspace_id):
        return next((grant for grant in self.workspaces if grant.workspace_id == workspace_id), None)

    @property
    def role_labels(self):
        labels = {"Tenant Admin"} if self.tenant_admin else set()
        if self.capacity_ids:
            labels.add("Capacity Admin")
        labels.update("Workspace Admin" if grant.role == "Admin" else grant.role for grant in self.workspaces)
        return sorted(labels) or ["Read-only"]


@dataclass(frozen=True)
class Resource:
    tenant_id: str
    workspace_id: str = ""
    capacity_id: str = ""
    item_id: str = ""
    repository_id: str = ""


def authorize(principal, action, resource):
    if not principal.tenant_id or principal.tenant_id != resource.tenant_id:
        raise AccessDenied("Cross-tenant access is denied.")
    if principal.expires_at is None or principal.expires_at <= datetime.now(timezone.utc):
        raise AccessDenied("Authorization expired. Refresh the session permissions.")
    if action == Action.TENANT_AUDIT:
        if principal.tenant_admin:
            return
        raise AccessDenied("Tenant administrator permission is required.")
    if action == Action.CAPACITY_READ:
        if resource.capacity_id and (principal.tenant_admin or resource.capacity_id in principal.capacity_ids):
            return
        raise AccessDenied("Capacity telemetry permission is required.")
    if action == Action.SCALE:
        if (resource.capacity_id in principal.capacity_ids
                and resource.capacity_id in principal.scalable_capacity_ids):
            return
        raise AccessDenied("Capacity administration and Azure capacity write permission are both required.")
    grant = principal.grant(resource.workspace_id)
    if grant is None:
        raise AccessDenied("The workspace is not assigned to this identity.")
    if action == Action.READ:
        return
    if action not in {Action.RUN, Action.CANCEL, Action.PULL_REQUEST}:
        raise AccessDenied("Unknown action.")
    if grant.role not in {"Admin", "Contributor"}:
        raise AccessDenied("Explicit Workspace Admin or Contributor permission is required.")
    if action == Action.PULL_REQUEST and resource.repository_id not in principal.repository_ids:
        raise AccessDenied("Repository write permission is required.")


def allowed(principal, action, resource):
    try:
        authorize(principal, action, resource)
        return True
    except AccessDenied:
        return False


def visible_records(principal, records):
    output = []
    for record in records:
        resource = Resource(record.get("tenant_id", ""), record.get("workspace_id", ""), record.get("capacity_id", ""))
        level = record.get("level", "item")
        action = Action.TENANT_AUDIT if level == "tenant" else Action.CAPACITY_READ if level == "capacity" else Action.READ
        if allowed(principal, action, resource):
            output.append(record)
    return output