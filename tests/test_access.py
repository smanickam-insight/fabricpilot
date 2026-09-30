from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from services.access import Action, AccessDenied, Principal, Resource, WorkspaceGrant, authorize, visible_records


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.principal = Principal("tenant-a", "user-a", "Reader",
                                   (WorkspaceGrant("workspace-a", "Finance"),),
                                   expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        self.resource = Resource("tenant-a", "workspace-a", "capacity-a")

    def test_cross_tenant_denied_even_for_admin(self):
        principal = replace(self.principal, tenant_admin=True)
        with self.assertRaises(AccessDenied):
            authorize(principal, Action.TENANT_AUDIT, Resource("tenant-b"))

    def test_viewer_cannot_run_or_cancel(self):
        authorize(self.principal, Action.READ, self.resource)
        for action in (Action.RUN, Action.CANCEL, Action.SCALE, Action.PULL_REQUEST):
            with self.assertRaises(AccessDenied):
                authorize(self.principal, action, self.resource)

    def test_workspace_admin_cannot_scale_capacity(self):
        principal = replace(self.principal, workspaces=(WorkspaceGrant("workspace-a", "Finance", "Admin"),))
        authorize(principal, Action.RUN, self.resource)
        with self.assertRaises(AccessDenied):
            authorize(principal, Action.SCALE, self.resource)

    def test_scale_requires_both_grants(self):
        principal = replace(self.principal, capacity_ids=frozenset({"capacity-a"}))
        with self.assertRaises(AccessDenied):
            authorize(principal, Action.SCALE, self.resource)
        authorize(replace(principal, scalable_capacity_ids=frozenset({"capacity-a"})), Action.SCALE, self.resource)

    def test_expired_grants_fail_closed(self):
        for expires in (None, datetime.now(timezone.utc) - timedelta(seconds=1)):
            with self.assertRaises(AccessDenied):
                authorize(replace(self.principal, expires_at=expires), Action.READ, self.resource)

    def test_tenant_admin_does_not_inherit_workspace_write(self):
        with self.assertRaises(AccessDenied):
            authorize(replace(self.principal, tenant_admin=True), Action.RUN, self.resource)

    def test_scope_filter_removes_other_tenants_and_workspaces(self):
        records = [
            {"tenant_id": "tenant-a", "workspace_id": "workspace-a", "id": "visible"},
            {"tenant_id": "tenant-b", "workspace_id": "workspace-a", "id": "foreign"},
            {"tenant_id": "tenant-a", "workspace_id": "workspace-b", "id": "unassigned"},
            {"tenant_id": "tenant-a", "level": "tenant", "id": "tenant-only"},
        ]
        self.assertEqual([record["id"] for record in visible_records(self.principal, records)], ["visible"])