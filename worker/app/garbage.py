"""Repeated sweeps of abandoned claim-specific object prefixes.

Attempt records deliberately outlive their jobs and media metadata: a PUT that
was already in flight when its writer lost the DB session can arrive after an
earlier cleanup. Retaining the record makes that late object collectible.
"""
import logging
import re

log = logging.getLogger(__name__)


class _SessionUnavailable(RuntimeError):
    pass


def _execute(conn, sql, parameters=None):
    try:
        return conn.execute(sql, parameters) if parameters is not None else conn.execute(sql)
    except Exception:
        # A failed statement may mean the session advisory lock is gone. Never
        # continue on a new connection or expose an exception's credential text.
        raise _SessionUnavailable() from None


def _fetch(conn, sql, parameters=None, many=False):
    try:
        cursor = _execute(conn, sql, parameters)
        return cursor.fetchall() if many else cursor.fetchone()
    except Exception:
        raise _SessionUnavailable() from None


def _session(conn, expected_pid=None):
    row = _fetch(conn, "SELECT 1 AS alive, pg_backend_pid() AS backend_pid")
    if not row or row["alive"] != 1 or (expected_pid is not None and row["backend_pid"] != expected_pid):
        raise _SessionUnavailable()
    return row["backend_pid"]


def _protected(conn, attempt, session_pid):
    _session(conn, session_pid)
    # Escape LIKE wildcards in a configured pipeline version. The stored key
    # and prefix are relative; only the S3 request receives the storage prefix.
    prefix = attempt["object_prefix"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = _fetch(conn, """
        SELECT EXISTS (
            SELECT 1 FROM media_processing_jobs
            WHERE id=%s AND media_id=%s AND claim_token=%s
              AND status='RUNNING' AND lease_until>now()
        ) AS active, EXISTS (
            SELECT 1 FROM media_variants
            WHERE media_id=%s AND object_key LIKE %s ESCAPE '\\'
        ) AS referenced
        """, (attempt["job_id"], attempt["media_id"], attempt["claim_token"],
              attempt["media_id"], prefix + "%"))
    if not row:
        raise _SessionUnavailable()
    return bool(row["active"] or row["referenced"])


def _valid_prefix(attempt):
    prefix = attempt["object_prefix"]
    pattern = (rf"media/{int(attempt['media_id'])}/v[1-9][0-9]*/[^/]+/"
               rf"{re.escape(str(attempt['claim_token']))}/")
    return isinstance(prefix, str) and re.fullmatch(pattern, prefix) is not None


def _sweep(conn, attempt, session_pid, s3, bucket, object_key):
    if _protected(conn, attempt, session_pid):
        return "protected"
    prefix = object_key(attempt["object_prefix"])
    if not prefix or not prefix.endswith(attempt["object_prefix"]):
        raise ValueError("Invalid storage prefix mapping")
    pages = iter(s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix))
    while True:
        if _protected(conn, attempt, session_pid):
            return "protected"
        try:
            page = next(pages)
        except StopIteration:
            return "swept"
        # A listing can block while the dedicated DB session loses its lock.
        if _protected(conn, attempt, session_pid):
            return "protected"
        for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if not item["Key"].startswith(prefix):
                raise ValueError("Object listing escaped the registered prefix")
            # Check the original session immediately before every external
            # delete. Acquiring another advisory lock here would hide lock loss.
            if _protected(conn, attempt, session_pid):
                return "protected"
            s3.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])


def collect_garbage(conn_factory, s3, bucket, object_key, batch_size=100):
    """Collect at most 100 due attempts on one direct, non-reconnecting session.

    ``object_key`` maps a relative key to its configured S3 storage prefix. The
    connection factory must provide an autocommit direct PostgreSQL connection;
    transaction-pooling proxies cannot preserve session advisory locks.
    """
    counts = {"examined": 0, "swept": 0, "protected": 0, "lock_missed": 0, "failed": 0}
    try:
        with conn_factory() as conn:
            session_pid = _session(conn)
            attempts = _fetch(conn, """
                SELECT claim_token,job_id,media_id,object_prefix FROM media_object_attempts
                WHERE next_cleanup_at<=now() ORDER BY next_cleanup_at,claim_token LIMIT %s
                """, (max(1, min(100, int(batch_size))),), many=True)
            for attempt in attempts:
                counts["examined"] += 1
                if not _valid_prefix(attempt):
                    counts["failed"] += 1
                    log.error("Object garbage collection skipped an invalid attempt prefix")
                    continue
                locked = _fetch(conn, "SELECT pg_try_advisory_lock(%s) AS locked",
                                (attempt["media_id"],))
                if not locked or not locked["locked"]:
                    # Active media can otherwise occupy the oldest due batch
                    # forever. Only defer registry bookkeeping; no media lock
                    # is required because this cannot delete or publish data.
                    _session(conn, session_pid)
                    _execute(conn, """
                        UPDATE media_object_attempts SET next_cleanup_at=now()+interval '1 minute'
                        WHERE claim_token=%s
                        """, (attempt["claim_token"],))
                    counts["lock_missed"] += 1
                    continue
                session_lost = False
                try:
                    outcome = _sweep(conn, attempt, session_pid, s3, bucket, object_key)
                    _session(conn, session_pid)
                    # A live writer whose session disappeared can remain RUNNING
                    # until its renewable lease expires. Revisit soon so an old
                    # claim's unreferenced PUTs are collected after recovery.
                    active = _fetch(conn, """
                        SELECT EXISTS (SELECT 1 FROM media_processing_jobs
                        WHERE id=%s AND claim_token=%s AND status='RUNNING' AND lease_until>now()) AS active
                        """, (attempt["job_id"], attempt["claim_token"]))
                    delay = 60 if active and active["active"] else 3600
                    _execute(conn, """
                        UPDATE media_object_attempts SET next_cleanup_at=now()+(%s*interval '1 second')
                        WHERE claim_token=%s
                        """, (delay, attempt["claim_token"]))
                    counts[outcome] += 1
                except _SessionUnavailable:
                    session_lost = True
                    raise
                except Exception as error:
                    counts["failed"] += 1
                    log.warning("Object garbage collection failed (%s)", type(error).__name__)
                finally:
                    if not session_lost:
                        _execute(conn, "SELECT pg_advisory_unlock(%s)", (attempt["media_id"],))
    except _SessionUnavailable:
        counts["failed"] += 1
        log.warning("Object garbage collection stopped: database session unavailable")
    except Exception as error:
        counts["failed"] += 1
        log.warning("Object garbage collection could not start (%s)", type(error).__name__)
    return counts
