import unittest
import uuid
from app.consumer import parse_message


class MessageValidationTest(unittest.TestCase):
    def test_valid_message(self):
        key=uuid.uuid4()
        parsed,payload=parse_message('{"jobId":"'+str(key)+'","traceparent":"trace"}')
        self.assertEqual(parsed,key)
        self.assertEqual(payload['traceparent'],'trace')

    def test_poison_messages_are_rejectable_without_crashing_consumer(self):
        for body in ('{"jobId":123}','{"jobId":{}}','{"jobId":null}','[]','null','{}','{"jobId":"invalid"}','not-json'):
            with self.subTest(body=body),self.assertRaises(ValueError): parse_message(body)


class ConsumerLifecycleTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock, patch
        from app.consumer import Consumer
        self.pool_patch = patch("app.consumer.ThreadPoolExecutor")
        self.pool = self.pool_patch.start().return_value
        self.addCleanup(self.pool_patch.stop)
        self.consumer = Consumer(MagicMock())
        self.channel = MagicMock()
        self.connection = MagicMock()
        self.connection.is_open = True
        self.consumer.connection = self.connection

    def deliver(self, tag=1):
        from unittest.mock import MagicMock
        self.consumer.consume(self.channel, MagicMock(delivery_tag=tag), MagicMock(headers={}),
                              '{"jobId":"' + str(uuid.uuid4()) + '"}')

    def test_two_queue_callbacks_are_tracked_without_overwriting_inflight_job(self):
        from concurrent.futures import Future
        first, second = Future(), Future()
        first.set_running_or_notify_cancel()
        self.pool.submit.side_effect = [first, second]
        self.deliver(1)
        self.deliver(2)
        self.assertEqual(len(self.consumer.futures), 2)
        self.consumer.cancel_pending()
        self.assertFalse(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertTrue(self.consumer.has_work())
        first.set_result(True)
        self.assertFalse(self.consumer.has_work())
        self.assertEqual(self.connection.add_callback_threadsafe.call_count, 2)

    def test_completion_cannot_ack_delivery_on_replacement_connection(self):
        from concurrent.futures import Future
        from unittest.mock import MagicMock
        future = Future()
        self.pool.submit.return_value = future
        self.deliver(42)
        replacement = MagicMock()
        self.consumer.connection = replacement
        future.set_result(True)
        replacement.add_callback_threadsafe.assert_not_called()
        self.connection.add_callback_threadsafe.call_args.args[0]()
        self.channel.basic_ack.assert_called_once_with(42)

    def test_failed_durable_state_save_nacks_for_redelivery(self):
        from concurrent.futures import Future
        future = Future()
        self.pool.submit.return_value = future
        self.deliver(9)
        future.set_exception(OSError("database offline"))
        self.connection.add_callback_threadsafe.call_args.args[0]()
        self.connection.call_later.assert_called_once()
        self.connection.call_later.call_args.args[1]()
        self.channel.basic_nack.assert_called_once_with(9, requeue=True)

    def test_shutdown_never_accepts_new_work(self):
        self.consumer.request_stop()
        self.deliver(3)
        self.pool.submit.assert_not_called()
        self.channel.basic_nack.assert_called_once_with(3, requeue=True)

    def test_grace_deadline_leaves_active_job_for_durable_recovery(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        future = Future()
        future.set_running_or_notify_cancel()
        self.pool.submit.return_value = future
        self.deliver()
        with patch.dict("os.environ", {"WORKER_SHUTDOWN_GRACE_SECONDS": "0"}):
            self.assertFalse(self.consumer.drain())
        self.assertFalse(future.cancelled())
        self.channel.basic_ack.assert_not_called()
        future.set_result(True)

    def test_per_consumer_qos_supports_quorum_queues_and_clean_term(self):
        from unittest.mock import patch
        self.connection.channel.return_value = self.channel
        self.connection.process_data_events.side_effect = lambda **_: self.consumer.request_stop()
        with patch("app.consumer.pika.BlockingConnection", return_value=self.connection), \
             patch("app.consumer.rabbit_parameters"):
            self.consumer.run()
        self.channel.basic_qos.assert_called_once_with(prefetch_count=1, global_qos=False)
        self.assertEqual(self.channel.basic_consume.call_count, 2)
        self.assertEqual(self.channel.basic_cancel.call_count, 2)
        self.connection.close.assert_called_once()
        self.pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)

    def test_broker_cancelled_subscription_cannot_remain_ready(self):
        from unittest.mock import patch
        self.connection.channel.return_value = self.channel
        def cancel_consumer(**_):
            self.channel.add_on_cancel_callback.call_args.args[0](None)
            self.consumer.request_stop()
        self.connection.process_data_events.side_effect = cancel_consumer
        with patch("app.consumer.pika.BlockingConnection", return_value=self.connection), \
             patch("app.consumer.rabbit_parameters"), \
             patch("app.consumer.mark_connected") as mark_ready, \
             patch("app.consumer.mark_disconnected") as mark_unready:
            self.consumer.run()
        mark_ready.assert_not_called()
        self.assertGreaterEqual(mark_unready.call_count, 2)
        self.connection.close.assert_called_once()
