from dataclasses import replace
import unittest

from services.assessment import assessments, incidents, scorecard
from services.demo_estate import demo_principal, demo_records


class AssessmentTests(unittest.TestCase):
    def test_missing_telemetry_is_not_a_pass(self):
        record = demo_records()[4] | {"evidence": {}}
        results = assessments(demo_principal(), [record])
        self.assertTrue(all(result["status"] == "Not assessed" for result in results))
        self.assertIsNone(scorecard(results)["score"])

    def test_all_six_playbooks_have_sample_triggers(self):
        found = {incident["playbook"] for incident in incidents(demo_principal(), demo_records())}
        self.assertEqual(found, {"capacity-shield", "spark-oom", "delta-lock", "directlake-recovery", "zombie-reaper", "freshness"})

    def test_thresholds_and_baseline_requirements(self):
        record = demo_records()[8]
        for evidence in [{"baseline_days": 29}, {"elapsed_seconds": 840}, {"status": "Completed"}]:
            updated = record | {"evidence": record["evidence"] | evidence}
            self.assertFalse(incidents(demo_principal(), [updated]))

    def test_scopes_apply_before_assessment(self):
        principal = replace(demo_principal(), tenant_admin=False, capacity_ids=frozenset(),
                            workspaces=(demo_principal().workspaces[0],))
        results = assessments(principal, demo_records())
        self.assertTrue(results)
        self.assertTrue(all(result["workspace_id"] == "demo-finance" for result in results))

    def test_insufficient_capacity_history_is_unknown(self):
        record = demo_records()[1]
        record = record | {"evidence": record["evidence"] | {"history_days": 7}}
        result = next(result for result in assessments(demo_principal(), [record]) if result["rule_id"] == "capacity-size")
        self.assertEqual(result["status"], "Not assessed")