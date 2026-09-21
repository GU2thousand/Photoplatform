"""Cloud security boundaries and local compatibility without live AWS services."""
import os
import ssl
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from app.config import database_parameters, rabbit_parameters, storage_client
from app.runtime import connection, fault_after_s3


class CloudConfigTests(unittest.TestCase):
    def test_aws_storage_uses_refreshable_default_credential_chain(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "aws", "STORAGE_ENDPOINT": "http://local:9000",
                                    "STORAGE_ACCESS_KEY": "do-not-pass", "STORAGE_SECRET_KEY": "do-not-pass"}, clear=True), \
             patch("app.config.boto3.client") as client:
            storage_client()
        args = client.call_args.kwargs
        self.assertNotIn("endpoint_url", args)
        self.assertNotIn("aws_access_key_id", args)
        self.assertNotIn("aws_secret_access_key", args)
        self.assertEqual(args["config"].s3["addressing_style"], "virtual")
        self.assertEqual(args["config"].retries["total_max_attempts"], 3)

    def test_minio_explicit_endpoint_credentials_and_path_style_remain(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "minio", "STORAGE_ENDPOINT": "http://minio:9000",
                                    "STORAGE_ACCESS_KEY": "local", "STORAGE_SECRET_KEY": "secret"}, clear=True), \
             patch("app.config.boto3.client") as client:
            storage_client()
        self.assertEqual(client.call_args.kwargs["endpoint_url"], "http://minio:9000")
        self.assertEqual(client.call_args.kwargs["aws_access_key_id"], "local")
        self.assertEqual(client.call_args.kwargs["config"].s3["addressing_style"], "path")

    def test_rds_credentials_are_fields_with_verified_tls(self):
        env = {"STORAGE_PROVIDER": "aws", "DATABASE_HOST": "db.example", "DATABASE_USER": "worker",
               "DATABASE_PASSWORD": "p@:/%? secret", "DATABASE_NAME": "photos"}
        with patch.dict(os.environ, env, clear=True):
            params = database_parameters()
        self.assertEqual(params["password"], env["DATABASE_PASSWORD"])
        self.assertEqual(params["sslmode"], "verify-full")
        self.assertEqual(params["sslrootcert"], "/app/certs/global-bundle.pem")
        self.assertEqual(params["options"], "-c statement_timeout=30000")

    def test_aws_cannot_downgrade_database_tls(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "aws", "DATABASE_URL": "postgres://test", "DATABASE_SSL_MODE": "require"}, clear=True), self.assertRaises(ValueError):
            database_parameters()

    def test_local_database_url_preserves_legacy_contract(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://local/db"}, clear=True):
            params = database_parameters()
        self.assertNotIn("host", params)
        self.assertNotIn("sslmode", params)  # libpq default or explicit DSN mode is preserved.

    def test_amazon_mq_separate_password_avoids_url_encoding_and_verifies_hostname(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "aws", "RABBITMQ_HOST": "broker.example",
                                    "RABBITMQ_USER": "worker", "RABBITMQ_PASSWORD": "p@:/%? secret"}, clear=True):
            params = rabbit_parameters()
        self.assertEqual(params.port, 5671)
        self.assertEqual(params.credentials.password, "p@:/%? secret")
        self.assertEqual(params.ssl_options.server_hostname, "broker.example")
        self.assertTrue(params.ssl_options.context.check_hostname)
        self.assertEqual(params.ssl_options.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(params.connection_attempts, 1)
        self.assertEqual(params.heartbeat, 60)

    def test_amqps_url_still_verifies_hostname(self):
        with patch.dict(os.environ, {"RABBITMQ_URL": "amqps://user:password@broker.example/%2F"}, clear=True):
            params = rabbit_parameters()
        self.assertTrue(params.ssl_options.context.check_hostname)
        self.assertEqual(params.ssl_options.server_hostname, "broker.example")

    def test_aws_rejects_plaintext_broker_url(self):
        with patch.dict(os.environ, {"STORAGE_PROVIDER": "aws", "RABBITMQ_URL": "amqp://guest:guest@local/"}, clear=True), self.assertRaises(ValueError):
            rabbit_parameters()

    def test_db_connection_retry_is_bounded_and_does_not_replay_transactions(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://local/db"}, clear=True), \
             patch("app.runtime.psycopg.connect", side_effect=psycopg.OperationalError("offline")) as connect, \
             patch("app.runtime.time.sleep") as sleep, self.assertRaises(psycopg.OperationalError):
            connection()
        self.assertEqual(connect.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_db_acquisition_retry_then_success_closes_once(self):
        raw = MagicMock()
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://local/db"}, clear=True), \
             patch("app.runtime.psycopg.connect", side_effect=[psycopg.OperationalError("offline"), raw]) as connect, \
             patch("app.runtime.time.sleep"):
            with connection() as conn:
                conn.execute("SELECT 1")
        self.assertEqual(connect.call_count, 2)
        raw.execute.assert_called_once_with("SELECT 1")
        raw.__exit__.assert_called_once()

    def test_destructive_fault_requires_both_disposable_flag_and_exact_job(self):
        for env, calls in (({}, 0), ({"WORKER_FAULT_AFTER_S3_JOB_ID": "target"}, 0),
                           ({"DISPOSABLE_ENVIRONMENT": "true", "WORKER_FAULT_AFTER_S3_JOB_ID": "other"}, 0),
                           ({"DISPOSABLE_ENVIRONMENT": "true", "WORKER_FAULT_AFTER_S3_JOB_ID": "target"}, 1)):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch("app.runtime.os._exit") as exit_process:
                fault_after_s3({"id": "target"})
                self.assertEqual(exit_process.call_count, calls)
                if calls:
                    exit_process.assert_called_once_with(86)
