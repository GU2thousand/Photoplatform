import hashlib
import io
import json
import logging
import os
import socket
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from prometheus_client import Counter, Gauge, Histogram
from opentelemetry.propagate import extract

from .imaging import InvalidImage, process_image
from .telemetry import configure

log = logging.getLogger(__name__)
tracer = configure("media-worker")
JOBS = Counter("media_jobs_total", "Jobs handled", ["type", "outcome"])
FAILURES = Counter("media_job_failures_total", "Processing failures", ["type", "reason"])
DURATION = Histogram("media_processing_duration_seconds", "Active processing time", ["type"], buckets=(.1,.5,1,2,5,10,30,60,120,300))
QUEUE_WAIT = Histogram("media_queue_wait_seconds", "Time from job creation to attempt", ["type"], buckets=(1,5,10,30,60,120,300,900))
END_TO_END = Histogram("media_end_to_end_seconds", "Upload session creation to media readiness", buckets=(1,5,10,30,60,120,300,900))
ACTIVE = Gauge("worker_active_jobs", "Jobs executing in this worker")
STORAGE_TIME = Histogram("storage_request_duration_seconds", "Storage request duration", ["operation"])
MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(15 * 1024 * 1024)))
MODEL_VERSION = os.getenv("CLIP_MODEL_VERSION", "clip-vit-b32-openai-v1")


def connection():
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, connect_timeout=5)


def object_key(relative):
    return "/".join(filter(None, [os.getenv("STORAGE_PREFIX", "generate-cloud").strip("/"), relative]))


def storage_client():
    return boto3.client("s3", endpoint_url=os.getenv("STORAGE_ENDPOINT") or None,
        region_name=os.getenv("STORAGE_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("STORAGE_ACCESS_KEY"), aws_secret_access_key=os.getenv("STORAGE_SECRET_KEY"),
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
                      retries={"max_attempts": 2}, s3={"addressing_style": "path" if os.getenv("STORAGE_PATH_STYLE_ACCESS", "true")=="true" else "virtual"}))


class Worker:
    def __init__(self):
        self.s3 = storage_client()
        self.bucket = os.environ["STORAGE_BUCKET"]
        self.encoder = None

    def read(self, key):
        with STORAGE_TIME.labels("get").time(), tracer.start_as_current_span("storage.download"):
            response = self.s3.get_object(Bucket=self.bucket, Key=object_key(key))
            with response["Body"] as stream:
                if response["ContentLength"] > MAX_BYTES:
                    raise InvalidImage("File exceeds byte limit")
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise InvalidImage("File exceeds byte limit")
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
                locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (job["media_id"],)).fetchone()["locked"]
                if not locked:
                    return True  # Durable outbox/lease watchdog will redeliver if still necessary.
                try:
                    claim = uuid.uuid4()
                    job = conn.execute("""
                        UPDATE media_processing_jobs SET status='RUNNING',claim_token=%s,attempt=attempt+1,
                          lease_until=now()+interval '5 minutes',updated_at=now(),started_at=now(),finished_at=NULL,worker_id=%s
                        WHERE id=%s AND ((status IN ('QUEUED','RETRY') AND next_attempt_at<=now())
                          OR (status='RUNNING' AND lease_until<now())) RETURNING *
                        """, (claim, socket.gethostname(), job_id)).fetchone()
                    if not job:
                        return True
                    span.set_attribute("media.id", job["media_id"])
                    span.set_attribute("job.type", job["job_type"])
                    QUEUE_WAIT.labels(job["job_type"]).observe(max(0,time.time()-job["created_at"].timestamp()))
                    try:
                        with DURATION.labels(job["job_type"]).time():
                            if job["job_type"] == "MEDIA_PROCESS": self.process(conn, job)
                            elif job["job_type"] == "EMBED": self.embed(conn, job)
                            elif job["job_type"] == "DELETE": self.delete(conn, job)
                        JOBS.labels(job["job_type"], "completed").inc()
                    except Exception as exc:
                        self.fail(conn, job, exc)
                    return True
                finally:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (media_id,))
                # The session closing also releases locks on every early-return/exception path.

    def finish(self, conn, job, status="DONE"):
        conn.execute("UPDATE media_processing_jobs SET status=%s,lease_until=NULL,last_error_code=NULL,updated_at=now(),finished_at=now() WHERE id=%s AND claim_token=%s",
                     (status,job["id"],job["claim_token"]))

    def process(self, conn, job):
        image = conn.execute("SELECT * FROM image_assets WHERE id=%s", (job["media_id"],)).fetchone()
        if image["deleted_at"] or image["asset_version"] != job["asset_version"]:
            self.finish(conn,job,"CANCELLED"); return
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
        rows = []
        for variant in variants:
            filename = "original" if variant.name == "original" else variant.name + ".webp"
            key = f"media/{image['id']}/v{job['asset_version']}/{job['pipeline_version']}/{filename}"
            with STORAGE_TIME.labels("put").time(), tracer.start_as_current_span("storage.variant"):
                self.s3.put_object(Bucket=self.bucket, Key=object_key(key), Body=variant.data,
                    ContentType=variant.content_type, CacheControl="public, max-age=31536000, immutable",
                    Metadata={"sha256":variant.sha256})
            rows.append((image["id"],job["asset_version"],variant.name,key,variant.content_type,len(variant.data),variant.width,variant.height,variant.sha256))
        with conn.transaction():
            current = conn.execute("SELECT deleted_at,asset_version FROM image_assets WHERE id=%s FOR UPDATE", (image["id"],)).fetchone()
            if current["deleted_at"] or current["asset_version"]!=job["asset_version"]:
                self.finish(conn,job,"CANCELLED"); return  # DELETE runs after this lock is released and cleans the entire prefix.
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
            self.finish(conn,job,"CANCELLED"); return
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
            if current["deleted_at"] or current["asset_version"]!=job["asset_version"]:
                self.finish(conn,job,"CANCELLED"); return
            conn.execute("""
                INSERT INTO media_embeddings(media_id,asset_version,model_version,embedding) VALUES(%s,%s,%s,%s::vector)
                ON CONFLICT(media_id,asset_version,model_version) DO UPDATE SET embedding=excluded.embedding,created_at=now()
                """,(image["id"],job["asset_version"],MODEL_VERSION,json.dumps(vector)))
            conn.execute("UPDATE image_assets SET embedding_status='READY',updated_at=now() WHERE id=%s",(image["id"],))
            self.finish(conn,job)

    def delete(self, conn, job):
        image = conn.execute("SELECT deleted_at FROM image_assets WHERE id=%s",(job["media_id"],)).fetchone()
        if not image or not image["deleted_at"]:
            self.finish(conn,job,"CANCELLED"); return
        prefix = object_key(f"media/{job['media_id']}/")
        with STORAGE_TIME.labels("delete").time(), tracer.start_as_current_span("storage.delete_variants"):
            for page in self.s3.get_paginator("list_object_versions").paginate(Bucket=self.bucket,Prefix=prefix):
                # Explicit version IDs also cover the "null" version of an unversioned
                # or suspended bucket. A key-only delete would only add a marker.
                for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                    self.s3.delete_object(Bucket=self.bucket, Key=item["Key"], VersionId=item["VersionId"])
        with conn.transaction():
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
            conn.execute("""
                UPDATE media_processing_jobs SET status=%s,last_error_code=%s,lease_until=NULL,
                  next_attempt_at=now()+(%s*interval '1 second'),updated_at=now(),finished_at=now() WHERE id=%s AND claim_token=%s AND status='RUNNING'
                """,(status,reason,min(300,5*2**min(job["attempt"],6)),job["id"],job["claim_token"]))
            conn.execute("UPDATE media_outbox SET last_published_at=NULL WHERE job_id=%s",(job["id"],))
            if dead and job["job_type"] in {"MEDIA_PROCESS","EMBED"}:
                field="processing_status" if job["job_type"]=="MEDIA_PROCESS" else "embedding_status"
                conn.execute(f"UPDATE image_assets SET {field}='FAILED',updated_at=now() WHERE id=%s AND deleted_at IS NULL",(job["media_id"],))
        FAILURES.labels(job["job_type"],reason).inc()
        JOBS.labels(job["job_type"],status.lower()).inc()
        log.warning("job=%s type=%s reason=%s attempt=%s status=%s",job["id"],job["job_type"],reason,job["attempt"],status)
