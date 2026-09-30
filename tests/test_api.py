from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app, identity
from services.demo_estate import demo_principal, demo_records
from services.operations import OperationStore


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = OperationStore(Path(self.temp.name) / "test.db")
        self.principal = demo_principal()
        self.database.put_records(self.principal, demo_records())
        app.dependency_overrides[identity] = lambda: (self.principal, None)
        self.patcher = patch("api.store", return_value=self.database)
        self.patcher.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.patcher.stop()
        app.dependency_overrides.clear()
        self.temp.cleanup()

    def test_assessments_and_cross_tenant_read(self):
        self.assertGreater(len(self.client.get("/v1/assessments").json()["checks"]), 20)
        self.principal = replace(self.principal, tenant_id="other")
        self.assertEqual(self.client.get("/v1/incidents").json(), [])

    def test_demo_execution_is_blocked_via_api(self):
        incident = next(item for item in self.client.get("/v1/incidents").json() if item["playbook"] == "delta-lock")
        plan = self.client.post("/v1/plans", json={"incident_id": incident["id"]}).json()["plan_id"]
        self.assertEqual(self.client.post(f"/v1/plans/{plan}/approve").status_code, 200)
        self.assertEqual(self.client.post(f"/v1/plans/{plan}/execute").status_code, 403)

    def test_client_cannot_inject_roles_or_evidence(self):
        self.assertEqual(self.client.post("/v1/plans", json={"incident_id": "fake", "tenant_admin": True}).status_code, 422)

    def test_anonymous_request_is_denied(self):
        app.dependency_overrides.clear()
        self.assertIn(self.client.get("/v1/me").status_code, (401, 403))