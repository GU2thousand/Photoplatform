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
