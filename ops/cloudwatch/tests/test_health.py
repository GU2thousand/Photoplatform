import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("collector", Path(__file__).resolve().parents[1] / "collector.py")
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "health.json"

    def test_missing_marker_is_unhealthy(self):
        self.assertFalse(collector.is_healthy(self.path, now=1000))

    def test_success_is_healthy_only_until_freshness_expires(self):
        collector.mark_healthy(1000, self.path)
        self.assertTrue(collector.is_healthy(self.path, now=1180))
        self.assertFalse(collector.is_healthy(self.path, now=1181))
        self.assertFalse(collector.is_healthy(self.path, now=999))

    def test_malformed_marker_is_unhealthy(self):
        for content in ("bad json", "{}", "[]", "null", '{"last_success": true}', '{"last_success": "1000"}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertFalse(collector.is_healthy(self.path, now=1000))

    def test_marker_removed_for_restart_and_shutdown(self):
        collector.mark_healthy(1000, self.path)
        collector.clear_health(self.path)
        collector.clear_health(self.path)
        self.assertFalse(self.path.exists())

    def test_run_marks_health_only_after_successful_publication(self):
        settings = collector.Settings("broker.example", "metrics", "secret", "cluster", "workers")
        stopped = Mock()
        stopped.is_set.side_effect = [False, False, True]
        subject = collector.Collector(settings, Mock(), Mock(), opener=Mock(), stopped=stopped,
                                      now=lambda: 1000, monotonic=lambda: 0)
        subject.collect_and_publish = Mock(side_effect=[collector.CollectionError("cloudwatch_unavailable"),
                                                        {"QueueDepth": 1, "ActiveTasks": 1}])
        with patch.object(collector, "mark_healthy") as mark:
            subject.run()
        mark.assert_called_once_with(1000)


if __name__ == "__main__":
    unittest.main()
