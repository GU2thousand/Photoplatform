"""Disposable-hook guards, markers and lease fencing without real waits."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

from app.test_hooks import barrier, validate_test_hooks


class VirtualClock:
    def __init__(self, on_sleep=None):
        self.now = 0
        self.on_sleep = on_sleep
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


class HookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.env = {"STORAGE_PROVIDER": "minio", "DISPOSABLE_ENVIRONMENT": "true",
                    "WORKER_TEST_HOOK_ENVIRONMENT": "kubernetes-local",
                    "WORKER_TEST_HOOK_DIR": str(self.path), "POD_NAME": "worker-test",
                    "POD_UID": "pod-test-uid"}
        env_patch = patch.dict(os.environ, self.env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.job = {"id": uuid.uuid4(), "media_id": 17, "claim_token": uuid.uuid4(), "_session_pid": 123}
        self.owned = MagicMock()

    def run_barrier(self, stage, clock, object_keys=None):
        with patch("app.test_hooks.time.monotonic", side_effect=clock.monotonic), \
             patch("app.test_hooks.time.sleep", side_effect=clock.sleep):
            barrier(self.job, stage, self.owned, object_keys)

    def marker(self, suffix):
        return self.path / f"{self.job['id']}.{suffix}"

    def test_no_configuration_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(validate_test_hooks())
            barrier(self.job, "before_write", self.owned)
        self.owned.assert_not_called()
        self.assertEqual(list(self.path.iterdir()), [])

    def test_aws_missing_disposable_nonlocal_and_unsafe_paths_are_rejected(self):
        cases = ({"STORAGE_PROVIDER": "aws"}, {"DISPOSABLE_ENVIRONMENT": "false"},
                 {"DISPOSABLE_ENVIRONMENT": ""}, {"WORKER_TEST_HOOK_ENVIRONMENT": "production"},
                 {"WORKER_TEST_HOOK_DIR": "/tmp"}, {"WORKER_TEST_HOOK_DIR": "relative/hooks"},
                 {"WORKER_TEST_HOOK_DIR": "/tmp/../etc/hooks"}, {"WORKER_TEST_HOOK_DIR": ""})
        for overrides in cases:
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides), \
                 self.assertRaises(ValueError):
                validate_test_hooks()
        with patch.dict(os.environ, {"WORKER_TEST_HOOK_UNKNOWN": "enabled"}, clear=True), \
             self.assertRaises(ValueError):
            validate_test_hooks()

    def test_timeout_is_bounded_and_default_is_900(self):
        self.assertEqual(validate_test_hooks()["timeout"], 900)
        for value in ("0", "1801", "nan", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"WORKER_TEST_HOOK_TIMEOUT_SECONDS": value}), \
                 self.assertRaises(ValueError):
                validate_test_hooks()
        with patch.dict(os.environ, {"WORKER_TEST_HOOK_ENVIRONMENT": "local"}):
            self.assertEqual(validate_test_hooks()["directory"], self.path.resolve())

    def test_after_write_without_block_records_stored_objects(self):
        keys = ["photos/media/17/v1/media-v1/claim/small.webp"]
        self.run_barrier("after_write", VirtualClock(), keys)
        payload = json.loads(self.marker("stored").read_text())
        self.assertEqual(payload["object_keys"], keys)
        self.assertEqual(payload["job_id"], str(self.job["id"]))
        self.assertEqual(payload["claim_token"], str(self.job["claim_token"]))
        self.assertEqual(payload["session_pid"], 123)
        self.assertEqual(payload["pod_name"], "worker-test")
        self.assertEqual(payload["pod_uid"], "pod-test-uid")
        self.owned.assert_called_once_with(self.job)
        self.assertFalse(self.marker("after_write.observed.json").exists())

    def test_each_supported_block_can_be_removed_to_release_with_regular_checks(self):
        for filename in ("before_write.block", f"{self.job['id']}.before_write.block",
                         "media-17.before_write.block"):
            with self.subTest(filename=filename):
                self.owned.reset_mock()
                block = self.path / filename
                block.touch()
                clock = VirtualClock(on_sleep=lambda: block.unlink(missing_ok=True))
                self.run_barrier("before_write", clock)
                payload = json.loads(self.marker("before_write.observed.json").read_text())
                self.assertEqual(payload["stage"], "before_write")
                self.assertEqual(payload["media_id"], 17)
                self.assertEqual(self.owned.call_count, 2)
                self.assertEqual(clock.sleeps, [1])

    def test_all_release_aliases_unblock_without_removing_block_file(self):
        block = self.path / "media-17.after_write.block"
        block.touch()
        for filename in (f"{self.job['id']}.release", "17.release", "media-17.release"):
            with self.subTest(filename=filename):
                release = self.path / filename
                clock = VirtualClock(on_sleep=lambda: release.touch())
                self.run_barrier("after_write", clock, ["photos/key"])
                self.assertEqual(clock.sleeps, [1])
                self.assertTrue(block.exists())
                release.unlink()

    def test_ownership_failure_is_rethrown_with_safe_fencing_marker(self):
        self.path.joinpath("before_write.block").touch()
        failure = RuntimeError("password=do-not-record")
        self.owned.side_effect = [None, failure]
        with self.assertRaises(RuntimeError) as caught:
            self.run_barrier("before_write", VirtualClock())
        self.assertIs(caught.exception, failure)
        text = self.marker("fenced.json").read_text()
        payload = json.loads(text)
        self.assertTrue(payload["fenced"])
        self.assertEqual(payload["exception_type"], "RuntimeError")
        self.assertEqual(payload["session_pid"], 123)
        self.assertNotIn("do-not-record", text)
        self.assertEqual(self.owned.call_count, 2)

    def test_initial_fencing_failure_never_emits_stored_or_observed_marker(self):
        failure = RuntimeError("claim lost")
        self.owned.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            self.run_barrier("after_write", VirtualClock())
        self.assertIs(caught.exception, failure)
        self.assertTrue(self.marker("fenced.json").exists())
        self.assertFalse(self.marker("stored").exists())
        self.assertFalse(self.marker("after_write.observed.json").exists())

    def test_monotonic_timeout_has_no_real_wait_and_does_not_claim_fencing(self):
        self.path.joinpath("before_write.block").touch()
        clock = VirtualClock()
        with patch.dict(os.environ, {"WORKER_TEST_HOOK_TIMEOUT_SECONDS": "2"}), \
             self.assertRaises(TimeoutError):
            self.run_barrier("before_write", clock)
        self.assertEqual(clock.sleeps, [1, 1])
        self.assertEqual(self.owned.call_count, 3)
        self.assertFalse(self.marker("fenced.json").exists())


if __name__ == "__main__":
    unittest.main()
