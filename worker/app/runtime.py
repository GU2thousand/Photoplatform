import hashlib
import io
import json
import logging
import os
import socket
import time
import uuid
import threading
from contextlib import contextmanager

from botocore.exceptions import ClientError
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from prometheus_client import Counter, Gauge, Histogram
from opentelemetry.propagate import extract

from .imaging import InvalidImage, process_image
from .telemetry import configure
from .config import database_parameters, storage_client

log = logging.getLogger(__name__)
tracer = configure("media-worker")
JOBS = Counter("media_jobs_total", "Jobs handled", ["type", "outcome"])
FAILURES = Counter("media_job_failures_total", "Processing failures", ["type", "reason"])
DURATION = Histogram("media_processing_duration_seconds", "Active processing time", ["type"], buckets=(.1,.5,1,2,5,10,30,60,120,300))
QUEUE_WAIT = Histogram("media_queue_wait_seconds", "Time from job creation to attempt", ["type"], buckets=(1,5,10,30,60,120,300,900))
END_TO_END = Histogram("media_end_to_end_seconds", "Upload session creation to media readiness", buckets=(1,5,10,30,60,120,300,900))
ACTIVE = Gauge("worker_active_jobs", "Jobs executing in this worker")
STORAGE_TIME = Histogram("storage_request_duration_seconds", "Storage request duration", ["operation"])
STORAGE_BYTES = Counter("worker_storage_bytes_total", "Successfully transferred payload bytes", ["operation"])
STORAGE_ERRORS = Counter("worker_storage_errors_total", "Storage operation failures", ["operation"])
DB_ERRORS = Counter("worker_database_errors_total", "Database failures", ["operation"])
DB_TIME = Histogram("worker_database_request_duration_seconds", "Client database operation latency", ["operation"], buckets=(.001,.005,.01,.025,.05,.1,.25,.5,1,5,30))
DB_CONNECTIONS = Gauge("worker_database_connections", "Open worker database connections")
MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(15 * 1024 * 1024)))
MODEL_VERSION = os.getenv("CLIP_MODEL_VERSION", "clip-vit-b32-openai-v1")


class LeaseLost(RuntimeError):
    """The original claim or its advisory-lock database session is no longer valid."""


class JobLease:
    """Renew on the *same* dedicated session which holds the advisory lock.

    A new connection would conceal the loss of the session lock. Never reconnect
    this session or use transaction-pooling proxies for this worker.
    """
    def __init__(self, conn, job, seconds=None, interval=None):
        self.conn, self.job = conn, job
        self.session_pid = job.get("_session_pid")
        self.seconds = seconds if seconds is not None else int(os.getenv("WORKER_LEASE_SECONDS", "300"))
        self.interval = interval if interval is not None else float(os.getenv("WORKER_LEASE_RENEW_SECONDS", "30"))
        if self.seconds < 10 or not 0 < self.interval <= self.seconds / 3:
            raise ValueError("Lease must be >=10 seconds with renewal interval <= one third")
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def renew(self):
        if self.lost.is_set():
            raise LeaseLost("Worker claim has been fenced")
        try:
            row = self.conn.execute("""
                UPDATE media_processing_jobs SET lease_until=now()+(%s*interval '1 second'),updated_at=now()
                WHERE id=%s AND claim_token=%s AND status='RUNNING' AND lease_until>now()
                RETURNING id,pg_backend_pid() AS backend_pid
                """, (self.seconds, self.job["id"], self.job["claim_token"])).fetchone()
            if not row:
                raise LeaseLost("Worker claim expired or was replaced")
            if self.session_pid is not None and row.get("backend_pid") != self.session_pid:
                raise LeaseLost("Worker advisory-lock database session changed")
        except Exception:
            self.lost.set()
            raise LeaseLost("Worker database session or claim is unavailable") from None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="job-lease", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                self.renew()
            except LeaseLost:
                log.warning("Job lease lost; subsequent writes are fenced")
                return

    def close(self):
        self.stop.set()
        if self.thread:
            # A blocked renew is bounded by the session's SQL timeout. Closing
            # the session after this guard ends cannot be confused with a new one.
            self.thread.join()


def assert_owned(job):
    lease = job.get("_lease")
    if lease:
        # Verify the original live session immediately before each external write.
        lease.renew()


def fence_transaction(conn, job):
    """Call after the image row lock: every metadata transaction uses that order."""
    if job.get("_lease"):
        row = conn.execute("""
            SELECT id,pg_backend_pid() AS backend_pid FROM media_processing_jobs WHERE id=%s AND claim_token=%s
              AND status='RUNNING' AND lease_until>now() FOR UPDATE
            """, (job["id"], job["claim_token"])).fetchone()
        if not row or (job.get("_session_pid") is not None and row.get("backend_pid") != job["_session_pid"]):
            job["_lease"].lost.set()
            raise LeaseLost("Metadata write fenced by expired or replaced claim")


class ObservedConnection:
    """Measure actual client operations without recording SQL or credential values."""
    def __init__(self, conn):
        self.conn = conn
        self.session_lock = threading.RLock()
        DB_CONNECTIONS.inc()

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            return self.conn.__exit__(*args)
        finally:
            DB_CONNECTIONS.dec()

    def execute(self, *args, **kwargs):
        with self.session_lock, DB_TIME.labels("query").time():
            try:
                return self.conn.execute(*args, **kwargs)
            except psycopg.Error:
                DB_ERRORS.labels("query").inc()
                raise

    @contextmanager
    def transaction(self):
        # psycopg serializes individual calls, but a renewal must never interleave
        # with a lifecycle transaction or commit work on another thread.
        with self.session_lock, self.conn.transaction():
            yield


def connection():
    # Only connection acquisition retries. Never blindly replay SQL transactions
    # after a disconnect: job claim tokens, leases and the outbox own recovery.
    attempts = max(1, min(5, int(os.getenv("DATABASE_CONNECT_ATTEMPTS", "3"))))
    for attempt in range(attempts):
        with DB_TIME.labels("connect").time():
            try:
                conn = psycopg.connect(os.getenv("DATABASE_URL", ""), autocommit=True,
                    row_factory=dict_row, **database_parameters())
                return ObservedConnection(conn)
            except psycopg.OperationalError:
                DB_ERRORS.labels("connect").inc()
                if attempt == attempts - 1:
                    raise
        time.sleep(min(4, 2 ** attempt))


def object_key(relative):
    return "/".join(filter(None, [os.getenv("STORAGE_PREFIX", "generate-cloud").strip("/"), relative]))


def fault_after_s3(job):
    """Explicit disposable-task fault; replacement task must omit the override."""
    if (os.getenv("DISPOSABLE_ENVIRONMENT", "false").lower() == "true"
            and os.getenv("WORKER_FAULT_AFTER_S3_JOB_ID") == str(job["id"])):
        log.warning("Injecting process loss after S3 writes for job=%s", job["id"])
        os._exit(86)


class Worker:
    def __init__(self):
        self.s3 = storage_client()
        self.bucket = os.environ["STORAGE_BUCKET"]
        self.encoder = None

    def collect_garbage(self):
        from .garbage import collect_garbage
        return collect_garbage(connection, self.s3, self.bucket, object_key)

    def read(self, key):
        with STORAGE_TIME.labels("get").time(), tracer.start_as_current_span("storage.download"):
            try:
                response = self.s3.get_object(Bucket=self.bucket, Key=object_key(key))
                with response["Body"] as stream:
                    if response["ContentLength"] > MAX_BYTES:
                        raise InvalidImage("File exceeds byte limit")
                    data = stream.read(MAX_BYTES + 1)
            except InvalidImage:
                raise
            except Exception:
                STORAGE_ERRORS.labels("get").inc()
                raise
            if len(data) > MAX_BYTES:
                raise InvalidImage("File exceeds byte limit")
            STORAGE_BYTES.labels("get").inc(len(data))
            return data

    def handle(self, job_id, headers=None):
        """Return False only if durable state could not be saved (broker must redeliver)."""
        with ACTIVE.track_inprogress(), tracer.start_as_current_span("media.job", context=extract(headers or {})) as span:
            span.set_attribute("job.id", str(job_id))
            with connection() as conn:
                job = conn.execute("SELECT * FROM media_processing_jobs WHERE id=%s", (job_id,)).fetchone()
                if not job or job["status"] in {"DONE", "CANCELLED", "DLQ"}:
                    return True
                # A session advisory lock spans external I/O without holding a database transaction.
                # DELETE uses the same lock, so a stale writer cannot recreate deleted objects.
                media_id = job["media_id"]
                lock_state = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked,pg_backend_pid() AS backend_pid", (job["media_id"],)).fetchone()
                if not lock_state["locked"]:
                    return True  # Durable outbox/lease watchdog will redeliver if still necessary.
                try:
                    seconds = int(os.getenv("WORKER_LEASE_SECONDS", "300"))
                    claim = uuid.uuid4()
                    job = conn.execute("""
                        UPDATE media_processing_jobs SET status='RUNNING',claim_token=%s,attempt=attempt+1,
                          lease_until=now()+(%s*interval '1 second'),updated_at=now(),started_at=now(),finished_at=NULL,worker_id=%s
                        WHERE id=%s AND ((status IN ('QUEUED','RETRY') AND next_attempt_at<=now())
                          OR (status='RUNNING' AND lease_until<now())) RETURNING *
                        """, (claim, seconds, socket.gethostname(), job_id)).fetchone()
                    if not job:
                        return True
                    job["_session_pid"] = lock_state.get("backend_pid")
                    span.set_attribute("media.id", job["media_id"])
                    span.set_attribute("job.type", job["job_type"])
                    QUEUE_WAIT.labels(job["job_type"]).observe(max(0,time.time()-job["created_at"].timestamp()))
                    lease = JobLease(conn, job, seconds=seconds)
                    job["_lease"] = lease
                    lease.start()
                    try:
                        with DURATION.labels(job["job_type"]).time():
                            if job["job_type"] == "MEDIA_PROCESS": outcome = self.process(conn, job)
                            elif job["job_type"] == "EMBED": outcome = self.embed(conn, job)
                            elif job["job_type"] == "DELETE": outcome = self.delete(conn, job)
                            else: raise InvalidImage("Unsupported job type")
                        JOBS.labels(job["job_type"], outcome or "completed").inc()
                    except LeaseLost:
                        return False  # No reconnect/write attempt from this stale session.
                    except psycopg.Error:
                        lease.lost.set()
                        raise  # The broker must redeliver; never save failure on another session.
                    except Exception as exc:
                        assert_owned(job)
                        self.fail(conn, job, exc)
                    finally:
                        lease.close()
                    return True
                finally:
                    try:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (media_id,))
                    except psycopg.Error:
                        # A disconnected session has already released its lock.
                        # Do not reconnect merely to unlock another session.
                        pass
                # The session closing also releases locks on every early-return/exception path.

    def finish(self, conn, job, status="DONE"):
        changed = conn.execute("UPDATE media_processing_jobs SET status=%s,lease_until=NULL,last_error_code=NULL,updated_at=now(),finished_at=now() WHERE id=%s AND claim_token=%s AND status='RUNNING' AND lease_until>now()",
                               (status,job["id"],job["claim_token"]))
        if changed.rowcount == 0:
            raise LeaseLost("Completion fenced by expired or replaced claim")

    def process(self, conn, job):
        image = conn.execute("SELECT * FROM image_assets WHERE id=%s", (job["media_id"],)).fetchone()
        if image["deleted_at"] or image["asset_version"] != job["asset_version"]:
            self.finish(conn,job,"CANCELLED"); return "cancelled"
        session = conn.execute("SELECT * FROM upload_sessions WHERE media_id=%s", (job["media_id"],)).fetchone()
        if not session:
            raise InvalidImage("Missing upload session")
        data = self.read(session["object_key"])
        if len(data) != session["expected_bytes"] or hashlib.sha256(data).hexdigest() != session["expected_sha256"]:
            raise InvalidImage("Content checksum or size mismatch")
        with tracer.start_as_current_span("image.decode_and_variants"):
            variants, phash, metadata = process_image(data)
        if variants[0].content_type != session["content_type"]:
            raise InvalidImage("Content type mismatch")
        assert_owned(job)
        attempt_prefix = f"media/{image['id']}/v{job['asset_version']}/{job['pipeline_version']}/{job['claim_token']}/"
        registered = conn.execute("""
            INSERT INTO media_object_attempts(claim_token,job_id,media_id,object_prefix)
            SELECT claim_token,id,media_id,%s FROM media_processing_jobs
            WHERE id=%s AND claim_token=%s AND status='RUNNING' AND lease_until>now()
            ON CONFLICT(claim_token) DO UPDATE SET object_prefix=excluded.object_prefix
            RETURNING claim_token
            """, (attempt_prefix, job["id"], job["claim_token"])).fetchone()
        if not registered:
            raise LeaseLost("Storage attempt registry write fenced by expired claim")
        rows = []
        for variant in variants:
            filename = "original" if variant.name == "original" else variant.name + ".webp"
            # A stale in-flight PUT can complete after DB-session loss. Isolate
            # attempts so it cannot overwrite objects published by a newer claim.
            key = attempt_prefix + filename
            assert_owned(job)
            with STORAGE_TIME.labels("put").time(), tracer.start_as_current_span("storage.variant"):
                try:
                    self.s3.put_object(Bucket=self.bucket, Key=object_key(key), Body=variant.data,
                        ContentType=variant.content_type, CacheControl="public, max-age=31536000, immutable",
                        Metadata={"sha256":variant.sha256})
                    STORAGE_BYTES.labels("put").inc(len(variant.data))
                except Exception:
                    STORAGE_ERRORS.labels("put").inc()
                    raise
            rows.append((image["id"],job["asset_version"],variant.name,key,variant.content_type,len(variant.data),variant.width,variant.height,variant.sha256))
        fault_after_s3(job)
        with conn.transaction():
            current = conn.execute("SELECT deleted_at,asset_version FROM image_assets WHERE id=%s FOR UPDATE", (image["id"],)).fetchone()
            fence_transaction(conn, job)
            if current["deleted_at"] or current["asset_version"]!=job["asset_version"]:
                self.finish(conn,job,"CANCELLED"); return "cancelled"  # DELETE cleans the prefix after this lock releases.
            for row in rows:
                conn.execute("""
                    INSERT INTO media_variants(media_id,asset_version,variant,object_key,content_type,size_bytes,width,height,content_sha256)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(media_id,asset_version,variant)
                    DO UPDATE SET object_key=excluded.object_key,content_type=excluded.content_type,size_bytes=excluded.size_bytes,
                      width=excluded.width,height=excluded.height,content_sha256=excluded.content_sha256
                    """,row)
            embeddings = os.getenv("SEMANTIC_SEARCH_ENABLED","false")=="true"
            conn.execute("""
                UPDATE image_assets SET processing_status='READY',embedding_status=%s,content_sha256=%s,
                  perceptual_hash=%s,width=%s,height=%s,safe_metadata=%s,updated_at=now() WHERE id=%s
                """,("QUEUED" if embeddings else "NOT_REQUESTED",variants[0].sha256,phash,variants[0].width,variants[0].height,Jsonb(metadata),image["id"]))
            if embeddings:
                embed_id = conn.execute("""
                    INSERT INTO media_processing_jobs(id,media_id,job_type,asset_version,pipeline_version,traceparent)
                    VALUES(%s,%s,'EMBED',%s,%s,%s) ON CONFLICT(media_id,job_type,asset_version,pipeline_version)
                    DO UPDATE SET updated_at=now() RETURNING id
                    """,(uuid.uuid4(),image["id"],job["asset_version"],MODEL_VERSION,job["traceparent"])).fetchone()["id"]
                conn.execute("INSERT INTO media_outbox(job_id) VALUES(%s) ON CONFLICT DO NOTHING",(embed_id,))
            self.finish(conn,job)
        END_TO_END.observe(max(0,time.time()-session["created_at"].timestamp()))

    def embed(self, conn, job):
        image = conn.execute("SELECT * FROM image_assets WHERE id=%s",(job["media_id"],)).fetchone()
        if image["deleted_at"] or image["asset_version"]!=job["asset_version"]:
            self.finish(conn,job,"CANCELLED"); return "cancelled"
        if job["pipeline_version"]!=MODEL_VERSION:
            raise InvalidImage("Model version mismatch")
        source = conn.execute("SELECT object_key FROM media_variants WHERE media_id=%s AND asset_version=%s AND variant='medium'",(image["id"],job["asset_version"])).fetchone()
        if not source: raise InvalidImage("Missing processed variant")
        if self.encoder is None:
            from .model import Encoder
            self.encoder = Encoder()
        with tracer.start_as_current_span("clip.image_embedding"):
            vector = self.encoder.image(self.read(source["object_key"]))
        with conn.transaction():
            current = conn.execute("SELECT deleted_at,asset_version FROM image_assets WHERE id=%s FOR UPDATE",(image["id"],)).fetchone()
            fence_transaction(conn, job)
            if current["deleted_at"] or current["asset_version"]!=job["asset_version"]:
                self.finish(conn,job,"CANCELLED"); return "cancelled"
            conn.execute("""
                INSERT INTO media_embeddings(media_id,asset_version,model_version,embedding) VALUES(%s,%s,%s,%s::vector)
                ON CONFLICT(media_id,asset_version,model_version) DO UPDATE SET embedding=excluded.embedding,created_at=now()
                """,(image["id"],job["asset_version"],MODEL_VERSION,json.dumps(vector)))
            conn.execute("UPDATE image_assets SET embedding_status='READY',updated_at=now() WHERE id=%s",(image["id"],))
            self.finish(conn,job)

    def delete(self, conn, job):
        image = conn.execute("SELECT deleted_at FROM image_assets WHERE id=%s",(job["media_id"],)).fetchone()
        if not image or not image["deleted_at"]:
            self.finish(conn,job,"CANCELLED"); return "cancelled"
        prefix = object_key(f"media/{job['media_id']}/")
        with STORAGE_TIME.labels("delete").time(), tracer.start_as_current_span("storage.delete_variants"):
            try:
                for page in self.s3.get_paginator("list_object_versions").paginate(Bucket=self.bucket,Prefix=prefix):
                    # Explicit version IDs also cover unversioned/suspended buckets.
                    for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                        assert_owned(job)
                        self.s3.delete_object(Bucket=self.bucket, Key=item["Key"], VersionId=item["VersionId"])
            except Exception:
                STORAGE_ERRORS.labels("delete").inc()
                raise
        with conn.transaction():
            conn.execute("SELECT id FROM image_assets WHERE id=%s FOR UPDATE", (job["media_id"],))
            fence_transaction(conn, job)
            conn.execute("DELETE FROM media_embeddings WHERE media_id=%s",(job["media_id"],))
            conn.execute("DELETE FROM media_variants WHERE media_id=%s",(job["media_id"],))
            conn.execute("UPDATE image_assets SET processing_status='DELETED',embedding_status='NOT_REQUESTED',updated_at=now() WHERE id=%s",(job["media_id"],))
            self.finish(conn,job)

    def fail(self, conn, job, exc):
        reason = "INVALID_IMAGE" if isinstance(exc,InvalidImage) else "PROCESSING_UNAVAILABLE"
        permanent = isinstance(exc,InvalidImage)
        if isinstance(exc,ClientError):
            missing=exc.response["Error"]["Code"] in {"NoSuchKey","404"}
            reason="OBJECT_MISSING" if missing else "STORAGE_UNAVAILABLE"
            permanent=missing
        # Erasure remains durable through arbitrarily long storage outages.
        # Permissions/object-lock failures also stay visible and retry with capped backoff.
        dead=job["job_type"] != "DELETE" and (permanent or job["attempt"]>=3)
        status="DLQ" if dead else "RETRY"
        with conn.transaction():
            # All lifecycle transactions lock image before job (API delete/retry and
            # successful processing use the same order), avoiding a delete/failure deadlock.
            conn.execute("SELECT id FROM image_assets WHERE id=%s FOR UPDATE",(job["media_id"],))
            fence_transaction(conn, job)
            changed = conn.execute("""
                UPDATE media_processing_jobs SET status=%s,last_error_code=%s,lease_until=NULL,
                  next_attempt_at=now()+(%s*interval '1 second'),updated_at=now(),finished_at=now() WHERE id=%s AND claim_token=%s AND status='RUNNING' AND lease_until>now()
                """,(status,reason,min(300,5*2**min(job["attempt"],6)),job["id"],job["claim_token"]))
            if changed.rowcount == 0:
                raise LeaseLost("Failure metadata fenced by expired or replaced claim")
            conn.execute("UPDATE media_outbox SET last_published_at=NULL WHERE job_id=%s",(job["id"],))
            if dead and job["job_type"] in {"MEDIA_PROCESS","EMBED"}:
                field="processing_status" if job["job_type"]=="MEDIA_PROCESS" else "embedding_status"
                conn.execute(f"UPDATE image_assets SET {field}='FAILED',updated_at=now() WHERE id=%s AND deleted_at IS NULL",(job["media_id"],))
        FAILURES.labels(job["job_type"],reason).inc()
        JOBS.labels(job["job_type"],status.lower()).inc()
        log.warning("job=%s type=%s reason=%s attempt=%s status=%s",job["id"],job["job_type"],reason,job["attempt"],status)
