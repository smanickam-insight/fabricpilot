"""Regression checks for the Fabric Pilot shell and illustrative capacity views."""

from pathlib import Path
from dataclasses import replace
from datetime import datetime, timezone
import unittest

from streamlit.testing.v1 import AppTest
from services.demo_estate import demo_principal, demo_records
from services.assessment import incidents
from components.capacity import capacity_projection, capacity_samples
from components.operations import simulation_matches


APP = Path(__file__).resolve().parents[1] / "app.py"


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = AppTest.from_file(str(APP), default_timeout=30).run()
        self.assertFalse(self.app.exception)

    def open_capacity(self):
        self.app.button(key="nav_monitoring").click().run()
        self.assertFalse(self.app.exception)

    def test_all_operations_routes(self):
        for icon in ["space_dashboard", "health_and_safety", "monitoring", "fact_check",
                     "visibility", "admin_panel_settings", "assignment", "auto_fix_high"]:
            self.app.button(key=f"nav_{icon}").click().run()
            self.assertFalse(self.app.exception)

    def test_details_open_and_close(self):
        self.open_capacity()
        for button, topic in [("inspect_consumption", "consumption"), ("inspect_sizing", "sizing"),
                              ("inspect_smoothing", "smoothing"), ("alert_delay", "delay"),
                              ("alert_burst", "burst"), ("alert_recovered", "recovered"),
                              ("metric_Average consumption", "consumption")]:
            self.app.button(key=button).click().run()
            self.assertFalse(self.app.exception)
            self.assertEqual(self.app.session_state["detail_topic"], topic)
            self.app.button(key="close_details").click().run()
            self.assertNotIn("detail_topic", self.app.session_state)

    def test_time_range_updates_metrics(self):
        self.open_capacity()
        original = self.app.button(key="metric_Average consumption").label
        for period in ["Last 7d", "Last 30d"]:
            self.app.selectbox(key="platform_timeframe").set_value(period).run()
            self.assertFalse(self.app.exception)
            self.assertNotEqual(self.app.button(key="metric_Average consumption").label, original)

    def test_profile_preserves_authentication_route(self):
        self.open_capacity()
        self.app.button(key="inspect_sizing").click().run()
        self.app.button(key="profile_login").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.session_state["pilot_modal"], "account")
        self.assertNotIn("detail_topic", self.app.session_state)

    def test_workspace_viewer_cannot_see_admin_navigation(self):
        principal = replace(demo_principal(), demo=False, tenant_admin=False, capacity_ids=frozenset(),
                            workspaces=(replace(demo_principal().workspaces[0], role="Viewer"),))
        self.app.session_state["authenticated"] = True
        self.app.session_state["platform_principal"] = principal
        self.app.session_state["platform_owner"] = (principal.tenant_id, principal.object_id, False)
        self.app.session_state["platform_records"] = [dict(record, observed_at=datetime.now(timezone.utc).isoformat()) for record in demo_records()]
        self.app.run()
        self.assertFalse(self.app.exception)
        keys = {button.key for button in self.app.button}
        self.assertNotIn("nav_admin_panel_settings", keys)
        self.assertNotIn("nav_monitoring", keys)
        self.app.button(key="nav_health_and_safety").click().run()
        table = next(table.value for table in self.app.dataframe if "Asset" in table.value)
        self.assertNotIn("Customer enrichment", table["Asset"].tolist())
        self.assertIn("Daily sales ingestion", table["Asset"].tolist())

    def test_workspace_filter_changes_incident_scope(self):
        self.app.selectbox(key="pilot_workspace_select").set_value("demo-finance").run()
        self.app.button(key="nav_health_and_safety").click().run()
        self.assertFalse(self.app.exception)
        table = next(table.value for table in self.app.dataframe if "Asset" in table.value)
        self.assertNotIn("Customer enrichment", table["Asset"].tolist())
        self.assertIn("Daily sales ingestion", table["Asset"].tolist())

    def test_context_drawer_is_disabled_until_simulation_policy_enabled(self):
        self.app.button(key="nav_health_and_safety").click().run()
        incident = next(incident for incident in incidents(demo_principal(), demo_records()) if incident["playbook"] == "delta-lock")
        self.app.session_state["pilot_detail"] = incident["id"]
        self.app.run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.button(key="pilot_execute").disabled)
        self.assertEqual(len(self.app.tabs), 2)
        self.app.session_state["pilot_policy_settings"] = {"delta-lock": {"mode": "Simulate only", "delays": [30, 60, 120]}}
        self.app.run()
        self.assertTrue(self.app.button(key="pilot_execute").disabled)
        self.app.checkbox(key="pilot_approved").check().run()
        self.assertFalse(self.app.button(key="pilot_execute").disabled)
        self.app.button(key="pilot_execute").click().run()
        self.assertEqual(self.app.session_state["pilot_audit"][0]["Outcome"], "Simulated; no external changes")
        self.app.button(key="pilot_close_detail").click().run()
        self.assertNotIn("pilot_detail", self.app.session_state)

    def test_overview_has_no_duplicate_navigation(self):
        self.assertFalse(self.app.segmented_control)
        self.assertEqual(len(self.app.selectbox), 3)

    def test_capacity_projection_is_calculated_from_samples(self):
        samples = capacity_samples("24 hours")
        result = capacity_projection(samples, 32, 0.18)
        self.assertAlmostEqual(result["monthly_savings"], 4204.8)
        self.assertAlmostEqual(result["over_target"], (samples.Consumption > 32).mean())
        self.assertEqual(capacity_projection(samples, 64, 0)["monthly_savings"], 0)

    def test_policy_threshold_changes_simulation_match(self):
        record = next(record for record in demo_records() if record["id"] == "ingest")
        self.assertTrue(simulation_matches("freshness", record, {"runtime_percent": 140}))
        self.assertFalse(simulation_matches("freshness", record, {"runtime_percent": 200}))

    def test_audit_query_filters_export_scope(self):
        self.app.button(key="nav_fact_check").click().run()
        self.app.text_input(key="pilot_architecture_query").set_value("Gold sales lakehouse").run()
        table = next(table.value for table in self.app.dataframe if "Scope / asset" in table.value)
        self.assertTrue(all("Gold sales lakehouse" in asset for asset in table["Scope / asset"]))

    def test_ignored_incident_remains_available(self):
        incident = incidents(demo_principal(), demo_records())[0]
        self.app.session_state["pilot_ignored"] = [incident["id"]]
        self.app.button(key="nav_health_and_safety").click().run()
        self.app.selectbox(key="pilot_incident_status").set_value("Ignored in session").run()
        table = next(table.value for table in self.app.dataframe if "Asset" in table.value)
        self.assertEqual(table["Asset"].tolist(), [incident["asset"]])


if __name__ == "__main__":
    unittest.main()