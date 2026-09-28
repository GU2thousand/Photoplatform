"""Release must not run Helm after a failed/stale migration identity."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("kind_runtime", Path(__file__).resolve().parents[1] / "kind_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

fault_spec = importlib.util.spec_from_file_location("kind_faults_for_budget", Path(__file__).resolve().parents[1] / "kind_tests.py")
faults = importlib.util.module_from_spec(fault_spec)
fault_spec.loader.exec_module(faults)


class MigrationGateTests(unittest.TestCase):
    def setUp(self):
        self.subject = runtime.Runtime.__new__(runtime.Runtime)
        self.subject.sha = "a" * 40
        self.subject.k = Mock()
        self.subject.stream = Mock()
        self.subject.helm_options = Mock(return_value=[])

    def test_failed_or_other_revision_migration_never_reaches_helm(self):
        for state in ({"sha": "a" * 40, "status": "FAIL"}, {"sha": "b" * 40, "status": "PASS"}):
            with self.assertRaises(ValueError):
                self.subject.deploy(state)
        self.subject.stream.assert_not_called()
        self.subject.k.assert_not_called()

    def test_success_record_cannot_reuse_replaced_job_uid(self):
        self.subject.k.return_value = '{"metadata":{"uid":"replacement"},"status":{"conditions":[{"type":"Complete","status":"True"}]}}'
        with self.assertRaises(ValueError):
            self.subject.deploy({"sha": "a" * 40, "status": "PASS", "name": "migration", "uid": "old"})
        self.subject.stream.assert_not_called()

    def test_success_record_cannot_rollout_an_incomplete_job(self):
        self.subject.k.return_value = '{"metadata":{"uid":"same"},"status":{"conditions":[]}}'
        with self.assertRaises(ValueError):
            self.subject.deploy({"sha": "a" * 40, "status": "PASS", "name": "migration", "uid": "same"})
        self.subject.stream.assert_not_called()

    def test_artifact_redaction_removes_credentials(self):
        value = runtime.sanitize("postgresql://user:credential@db/app password=other-secret token=jwt-value")
        for secret in ("credential", "other-secret", "jwt-value"):
            self.assertNotIn(secret, value)


class DependencyObservationBudgetTests(unittest.TestCase):
    def test_outage_exceeds_actual_worker_staleness_budget(self):
        self.assertEqual(faults.outage_observation_seconds(180), 210)
        self.assertEqual(faults.outage_observation_seconds(300), 330)
        self.assertGreater(faults.outage_observation_seconds(45), 45)

    def test_invalid_or_unbounded_heartbeat_budget_is_rejected(self):
        for value in (0, -1, 601, float("nan"), float("inf"), "180", True):
            with self.subTest(value=value), self.assertRaises(AssertionError):
                faults.outage_observation_seconds(value)


class MandatoryCaseAccountingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.subject = runtime.Runtime.__new__(runtime.Runtime)
        self.subject.evidence = Path(self.directory.name)
        self.subject.manifest = {}
        self.subject.save = Mock()

    def summary(self, name, count, failures=0):
        value = {"denominator": count, "passed": count - failures, "failed": failures, "skipped": 0,
                 "cases": [{"status": "FAIL" if index < failures else "PASS"} for index in range(count)]}
        (self.subject.evidence / name).write_text(json.dumps(value))

    def test_early_failure_keeps_unrun_extension_in_denominator(self):
        self.summary("kind-summary.json", 5, failures=1)
        self.subject.kind_case_totals()
        self.assertEqual(self.subject.manifest["kubernetes_case_totals"],
                         {"denominator": 9, "passed": 4, "failed": 1, "skipped": 0, "not_run": 4})

    def test_completed_suites_account_for_all_nine_cases(self):
        self.summary("kind-summary.json", 5)
        self.summary("kind-extended-summary.json", 4)
        self.subject.kind_case_totals()
        self.assertEqual(self.subject.manifest["kubernetes_case_totals"],
                         {"denominator": 9, "passed": 9, "failed": 0, "skipped": 0, "not_run": 0})

    def test_declared_pass_count_cannot_hide_a_failed_case(self):
        self.summary("kind-summary.json", 5, failures=1)
        path = self.subject.evidence / "kind-summary.json"
        value = json.loads(path.read_text())
        value["passed"], value["failed"] = 5, 0
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.subject.kind_case_totals()


if __name__ == "__main__":
    unittest.main()
