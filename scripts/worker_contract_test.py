#!/usr/bin/env python3
"""Direct PostgreSQL worker contracts in a uniquely named disposable schema.

Uses real libpq sessions/advisory locks and the exact V6 registry migration.
Storage is a deterministic double; this does not claim real S3 fault validation.
"""
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import MagicMock

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from app.garbage import collect_garbage
from app.runtime import JobLease, LeaseLost, ObservedConnection


class PostgreSQLWorkerContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.getenv("WORKER_CONTRACT_DATABASE_URL") or make_conninfo(
            host="127.0.0.1", port=os.getenv("POSTGRES_PORT", "5432"),
            dbname="generatecloud", user="generatecloud",
            password=os.getenv("POSTGRES_PASSWORD", "generatecloud"))
        cls.schema = "worker_contract_" + uuid.uuid4().hex
        with psycopg.connect(cls.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.addClassCleanup(cls.drop_schema)
        with cls.connect() as conn:
            # Only columns used by these worker contracts are necessary; all
            # names/types match the production job/variant tables.
            conn.execute("""
                CREATE TABLE media_processing_jobs (
                  id uuid PRIMARY KEY,media_id bigint NOT NULL,claim_token uuid,
                  status varchar(20) NOT NULL,lease_until timestamptz,
                  updated_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE media_variants (media_id bigint NOT NULL,object_key varchar(600) NOT NULL);
                """)
            migration = Path(__file__).resolve().parents[1] / "backend/src/main/resources/db/migration/V6__media_object_attempts.sql"
            conn.execute(migration.read_text())
            print("Disposable worker schema; PostgreSQL " + conn.execute("SHOW server_version").fetchone()["server_version"])

    @classmethod
    def connect(cls):
        return ObservedConnection(psycopg.connect(cls.dsn, autocommit=True,
            row_factory=dict_row, connect_timeout=3,
            options=f"-c search_path={cls.schema} -c statement_timeout=3000"))

    @classmethod
    def drop_schema(cls):
        with psycopg.connect(cls.dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.connect() as conn:
            conn.execute("TRUNCATE media_object_attempts,media_variants,media_processing_jobs")
        self.job = {"id": uuid.uuid4(), "media_id": 17, "claim_token": uuid.uuid4()}

    def insert_job(self, conn, seconds=10):
        conn.execute("""
            INSERT INTO media_processing_jobs(id,media_id,claim_token,status,lease_until)
            VALUES(%s,%s,%s,'RUNNING',now()+(%s*interval '1 second'))
            """, (self.job["id"], self.job["media_id"], self.job["claim_token"], seconds))

    def test_real_renewal_survives_original_short_lease_and_holds_session_lock(self):
        with self.connect() as conn, self.connect() as rival:
            self.insert_job(conn)
            self.assertTrue(conn.execute("SELECT pg_try_advisory_lock(17) AS locked").fetchone()["locked"])
            self.assertFalse(rival.execute("SELECT pg_try_advisory_lock(17) AS locked").fetchone()["locked"])
            original_pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            self.job["_session_pid"] = original_pid
            lease = JobLease(conn, self.job, seconds=10, interval=1)
            lease.start()
            try:
                time.sleep(11)  # Exceed the original real lease, without a six-minute CI delay.
                row = conn.execute("SELECT claim_token,lease_until>now() AS live FROM media_processing_jobs").fetchone()
                self.assertTrue(row["live"])
                self.assertEqual(row["claim_token"], self.job["claim_token"])
                self.assertEqual(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"], original_pid)
                self.assertFalse(lease.lost.is_set())
                self.assertFalse(rival.execute("SELECT pg_try_advisory_lock(17) AS locked").fetchone()["locked"])
            finally:
                lease.close()

    def test_replaced_and_expired_tokens_cannot_renew(self):
        with self.connect() as conn:
            self.insert_job(conn)
            lease = JobLease(conn, self.job, seconds=10, interval=1)
            conn.execute("UPDATE media_processing_jobs SET claim_token=%s", (uuid.uuid4(),))
            with self.assertRaises(LeaseLost):
                lease.renew()
            conn.execute("UPDATE media_processing_jobs SET claim_token=%s,lease_until=now()-interval '1 second'",
                         (self.job["claim_token"],))
            expired = JobLease(conn, self.job, seconds=10, interval=1)
            with self.assertRaises(LeaseLost):
                expired.renew()

    def test_server_session_loss_releases_advisory_lock_and_fences_old_guard(self):
        with self.connect() as conn, self.connect() as admin:
            self.insert_job(conn)
            conn.execute("SELECT pg_advisory_lock(17)")
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            lease = JobLease(conn, self.job, seconds=10, interval=1)
            self.assertTrue(admin.execute("SELECT pg_terminate_backend(%s) AS killed", (pid,)).fetchone()["killed"])
            with self.assertRaises(LeaseLost):
                lease.renew()
            self.assertTrue(lease.lost.is_set())
            self.assertTrue(admin.execute("SELECT pg_try_advisory_lock(17) AS locked").fetchone()["locked"])

    def test_v6_real_gc_sql_protects_references_and_sweeps_repeated_late_objects(self):
        prefix = f"media/17/v1/media_v1%literal/{self.job['claim_token']}/"
        with self.connect() as conn:
            self.insert_job(conn)
            conn.execute("UPDATE media_processing_jobs SET status='DONE',lease_until=NULL")
            conn.execute("""INSERT INTO media_object_attempts(claim_token,job_id,media_id,object_prefix,next_cleanup_at)
                            VALUES(%s,%s,17,%s,now()-interval '1 second')""",
                         (self.job["claim_token"], self.job["id"], prefix))
            conn.execute("INSERT INTO media_variants VALUES(17,%s)", (prefix + "medium.webp",))
        storage = MagicMock()
        mapper = lambda key: "test-prefix/" + key
        self.assertEqual(collect_garbage(self.connect, storage, "private", mapper)["protected"], 1)
        storage.get_paginator.assert_not_called()
        with self.connect() as conn:
            conn.execute("DELETE FROM media_variants")
        for version in ("first-wave", "late-wave"):
            with self.connect() as conn:
                conn.execute("UPDATE media_object_attempts SET next_cleanup_at=now()-interval '1 second'")
            storage.get_paginator.return_value.paginate.return_value = [{"Versions": [
                {"Key": mapper(prefix + "medium.webp"), "VersionId": version}]}]
            self.assertEqual(collect_garbage(self.connect, storage, "private", mapper)["swept"], 1)
            self.assertEqual(storage.delete_object.call_args.kwargs["VersionId"], version)
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM media_object_attempts").fetchone()["n"], 1)
            self.assertTrue(conn.execute("SELECT next_cleanup_at>now() AS deferred FROM media_object_attempts").fetchone()["deferred"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
