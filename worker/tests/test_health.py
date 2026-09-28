import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.health import healthy, live, live_path, mark_alive, mark_stopped, mark_connected, mark_disconnected


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'health.json'
        env = patch.dict(os.environ, {'WORKER_HEALTH_FILE': str(self.path),
                                    'WORKER_LIVE_FILE': str(self.path.with_name('live.json'))})
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

    def test_dependency_outage_is_unready_but_loop_remains_live(self):
        mark_alive()
        mark_connected()
        self.assertFalse(healthy(lambda: False))
        with patch('app.health.database_ready', side_effect=AssertionError('liveness contacted DB')):
            self.assertTrue(live())
        mark_disconnected()  # MQ outage does not remove the separate main-loop heartbeat.
        self.assertFalse(healthy(lambda: True))
        self.assertTrue(live())
        mark_stopped()
        self.assertFalse(live())

    def test_hung_or_absent_process_is_not_live(self):
        for timestamp in (time.time()-181, time.time()+30, float('nan')):
            with self.subTest(timestamp=timestamp):
                live_path().write_text(json.dumps({'updated_at': timestamp, 'pid': os.getpid()}))
                self.assertFalse(live())
        mark_alive()
        with patch('app.health.os.kill', side_effect=ProcessLookupError()):
            self.assertFalse(live())

    def test_liveness_tolerates_bounded_broker_rpc_and_reconnect_wait(self):
        live_path().write_text(json.dumps({'updated_at': time.time()-121, 'pid': os.getpid()}))
        self.assertTrue(live())
        self.assertFalse(healthy(lambda: True))

    def test_database_probe_has_connect_and_statement_timeouts(self):
        from app.health import database_ready
        with patch('app.health.database_parameters', return_value={}), patch('psycopg.connect') as connect:
            connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = (1,)
            self.assertTrue(database_ready())
        self.assertEqual(connect.call_args.kwargs['connect_timeout'], 3)
        self.assertEqual(connect.call_args.kwargs['options'], '-c statement_timeout=1000')
