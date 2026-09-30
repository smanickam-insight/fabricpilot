from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from services.access import AccessDenied
from services.assessment import incidents
from services.demo_estate import demo_principal, demo_records
from services.operations import OperationStore, execute_plan


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = OperationStore(Path(self.temp.name) / "pilot.db")
        self.principal = demo_principal()
        self.record = demo_records()[4]
        self.incident = incidents(self.principal, [self.record])[0]

    def test_plan_is_idempotent_and_tenant_bound(self):
        first = self.store.create_plan(self.principal, self.incident, self.record)
        self.assertEqual(first, self.store.create_plan(self.principal, self.incident, self.record))
        with self.assertRaises(AccessDenied):
            self.store.get_plan(replace(self.principal, tenant_id="other"), first)

    def test_demo_cannot_execute_even_when_enabled_and_approved(self):
        plan = self.store.create_plan(self.principal, self.incident, self.record)
        self.store.approve(self.principal, plan)
        credential = Mock()
        with self.assertRaises(AccessDenied):
            execute_plan(self.store, self.principal, plan, credential, enabled=True)
        credential.get_token.assert_not_called()
        self.assertEqual(len(self.store.audit(self.principal)), 2)

    def test_repeated_approval_and_unbounded_retries_rejected(self):
        plan = self.store.create_plan(self.principal, self.incident, self.record)
        self.store.approve(self.principal, plan)
        with self.assertRaises(ValueError):
            self.store.approve(self.principal, plan)
        with self.assertRaises(ValueError):
            self.store.create_plan(self.principal, self.incident, self.record, attempt=3)

    def test_snapshot_isolation(self):
        self.store.put_records(self.principal, demo_records())
        self.assertEqual(len(self.store.records(self.principal)), 10)
        self.assertEqual(self.store.records(replace(self.principal, tenant_id="other")), [])