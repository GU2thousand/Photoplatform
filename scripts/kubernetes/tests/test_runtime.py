"""Release must not run Helm after a failed/stale migration identity."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("kind_runtime", Path(__file__).resolve().parents[1] / "kind_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


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


if __name__ == "__main__":
    unittest.main()
