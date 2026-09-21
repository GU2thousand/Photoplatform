import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.health import healthy, mark_connected, mark_disconnected


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'health.json'
        env = patch.dict(os.environ, {'WORKER_HEALTH_FILE': str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_missing_file_is_not_ready(self):
        self.assertFalse(healthy(lambda: True))

    def test_fresh_broker_io_and_database_are_both_required(self):
        mark_connected()
        self.assertTrue(healthy(lambda: True))
        self.assertFalse(healthy(lambda: False))
        mark_disconnected()
        self.assertFalse(healthy(lambda: True))

    def test_stale_or_future_heartbeat_is_not_ready(self):
        for timestamp in (time.time()-16, time.time()+30, 'invalid', True, None, float('nan')):
            with self.subTest(timestamp=timestamp):
                self.path.write_text(json.dumps({'updated_at': timestamp}))
                self.assertFalse(healthy(lambda: True))

    def test_malformed_file_is_not_ready(self):
        for value in ('', '{}', '[]', 'not-json'):
            with self.subTest(value=value):
                self.path.write_text(value)
                self.assertFalse(healthy(lambda: True))
