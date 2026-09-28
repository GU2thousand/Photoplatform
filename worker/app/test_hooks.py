"""Explicit, filesystem-only barriers for disposable local failure tests."""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid


def validate_test_hooks():
    configured = any(name.startswith("WORKER_TEST_HOOK_") for name in os.environ)
    if not configured:
        return None
    if (os.getenv("STORAGE_PROVIDER", "").lower() != "minio"
            or os.getenv("DISPOSABLE_ENVIRONMENT", "").lower() != "true"
            or os.getenv("WORKER_TEST_HOOK_ENVIRONMENT") not in {"local", "kubernetes-local"}):
        raise ValueError("Worker test hooks require disposable local MinIO")
    directory = Path(os.getenv("WORKER_TEST_HOOK_DIR", ""))
    root = Path("/tmp").resolve()
    resolved = directory.resolve()
    if not directory.is_absolute() or resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Worker test hook directory must be below /tmp")
    try:
        timeout = int(os.getenv("WORKER_TEST_HOOK_TIMEOUT_SECONDS", "900"))
    except ValueError:
        raise ValueError("Invalid worker test hook timeout") from None
    if not 1 <= timeout <= 1800:
        raise ValueError("Worker test hook timeout must be 1..1800 seconds")
    return {"directory": resolved, "timeout": timeout}


def _write_marker(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".hook-",
                                         encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, sort_keys=True)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def barrier(job, stage, check_owned, object_keys=None):
    config = validate_test_hooks()
    if config is None:
        return
    if stage not in {"before_write", "after_write"}:
        raise ValueError("Unsupported worker test hook stage")
    directory = config["directory"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # UUID/int conversion prevents job fields from escaping marker filenames.
    job_id = str(uuid.UUID(str(job["id"])))
    media_id = int(job["media_id"])
    pod = os.getenv("POD_NAME", socket.gethostname())
    payload = {"job_id": job_id, "media_id": media_id,
               "claim_token": str(uuid.UUID(str(job["claim_token"]))), "pod": pod,
               "pod_name": pod, "pod_uid": os.getenv("POD_UID"),
               "session_pid": job.get("_session_pid"), "stage": stage,
               "object_keys": object_keys}

    def verify_owned():
        try:
            check_owned(job)
        except Exception as error:
            # Preserve the original fencing exception even if audit-marker IO
            # fails. Never record exception messages or credential-bearing SQL.
            try:
                _write_marker(directory / f"{job_id}.fenced.json",
                              {**payload, "fenced": True, "exception_type": type(error).__name__})
            except Exception:
                pass
            raise

    verify_owned()
    if stage == "after_write":
        _write_marker(directory / f"{job_id}.stored", payload)
    blocks = [directory / f"{stage}.block", directory / f"{job_id}.{stage}.block",
              directory / f"media-{media_id}.{stage}.block"]
    releases = [directory / f"{job_id}.release", directory / f"{media_id}.release",
                directory / f"media-{media_id}.release"]
    if not any(path.exists() for path in blocks):
        return
    _write_marker(directory / f"{job_id}.{stage}.observed.json", payload)
    deadline = time.monotonic() + config["timeout"]
    while any(path.exists() for path in blocks) and not any(path.exists() for path in releases):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Disposable worker test hook timed out")
        time.sleep(min(1, remaining))
        verify_owned()
