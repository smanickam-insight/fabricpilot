from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from services.access import AccessDenied, Resource
from services.agent_brain import diagnose
from services.demo_estate import demo_principal, demo_records
from services.devops import Repository, publish_pull_request
from services.telemetry import collect_kql
from services.capacity_actions import scale_capacity


class IntegrationTests(unittest.TestCase):
    def test_kql_rejects_unapproved_tenant_and_arbitrary_host(self):
        principal = replace(demo_principal(), demo=False)
        with self.assertRaises(AccessDenied):
            collect_kql(principal, Mock(), "https://cluster.kusto.windows.net", "db", set())
        with self.assertRaises(ValueError):
            collect_kql(principal, Mock(), "https://attacker.invalid", "db", {principal.tenant_id})

    def test_scaling_requires_arm_permission_and_budget(self):
        principal = replace(demo_principal(), demo=False)
        resource = Resource(principal.tenant_id, capacity_id="demo-f64")
        with self.assertRaises(AccessDenied):
            scale_capacity(principal, resource, Mock(), {}, "F128", approved=True, enabled=True)
        principal = replace(principal, scalable_capacity_ids=frozenset({"demo-f64"}))
        with self.assertRaises(AccessDenied):
            scale_capacity(principal, resource, Mock(), {}, "F128", approved=True, enabled=True)

    def test_demo_evidence_cannot_leave_for_ai(self):
        with self.assertRaises(AccessDenied):
            diagnose(demo_principal(), demo_records()[4])

    @patch("services.devops.requests.post")
    def test_pr_publication_requires_live_approval(self, post):
        principal = replace(demo_principal(), repository_ids=frozenset({"repo"}))
        resource = Resource("demo-tenant", "demo-finance", repository_id="repo")
        repository = Repository("demo-tenant", "org", "project", "repo")
        with self.assertRaises(AccessDenied):
            publish_pull_request(principal, resource, repository, Mock(), {"/job.py": "print('test')"}, "Test", approved=True, enabled=True)
        post.assert_not_called()

    def test_foreign_resource_cannot_reach_ai(self):
        record = demo_records()[4] | {"tenant_id": "other"}
        with self.assertRaises(AccessDenied):
            diagnose(demo_principal(), record)