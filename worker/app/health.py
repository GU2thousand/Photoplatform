"""Independent loop liveness and bounded broker/database readiness probes."""
import argparse
import json
import math
import os
from pathlib import Path
import time

from .config import database_parameters


def health_path():
    return Path(os.getenv("WORKER_HEALTH_FILE", "/tmp/photoplatform-worker-health.json"))


def live_path():
    return Path(os.getenv("WORKER_LIVE_FILE", "/tmp/photoplatform-worker-live.json"))


def write_heartbeat(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(path)


def mark_alive():
    write_heartbeat(live_path(), {"updated_at": time.time(), "pid": os.getpid()})


def mark_stopped():
    live_path().unlink(missing_ok=True)


def fresh(payload, maximum_age):
    timestamp = payload["updated_at"]
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        return False
    return -5 <= time.time() - timestamp <= maximum_age


def live():
    """No DB, broker, storage, or ML calls: external outages cannot fail liveness."""
    try:
        payload = json.loads(live_path().read_text())
        if not fresh(payload, float(os.getenv("WORKER_LIVE_MAX_AGE_SECONDS", "180"))):
            return False
        pid = payload["pid"]
        if type(pid) is not int or pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def mark_connected():
    write_heartbeat(health_path(), {"updated_at": time.time()})


def mark_disconnected():
    health_path().unlink(missing_ok=True)


def database_ready():
    import psycopg
    try:
        params = database_parameters()
        params.update(connect_timeout=3, options="-c statement_timeout=1000",
                      application_name="photoplatform-worker-health")
        with psycopg.connect(os.getenv("DATABASE_URL", ""), autocommit=True, **params) as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)
    except (psycopg.Error, OSError, ValueError, KeyError):
        return False


def healthy(probe=database_ready):
    try:
        payload = json.loads(health_path().read_text())
        if not fresh(payload, float(os.getenv("WORKER_HEALTH_MAX_AGE_SECONDS", "15"))):
            return False
        return bool(probe())
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("live", "readiness"), default="readiness")
    mode = parser.parse_args().mode
    raise SystemExit(0 if (live() if mode == "live" else healthy()) else 1)
