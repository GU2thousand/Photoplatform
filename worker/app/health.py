"""ECS readiness: fresh broker I/O heartbeat plus a bounded real DB probe."""
import json
import os
from pathlib import Path
import time

from .config import database_parameters


def health_path():
    return Path(os.getenv("WORKER_HEALTH_FILE", "/tmp/photoplatform-worker-health.json"))


def mark_connected():
    path = health_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"updated_at": time.time()}))
    temporary.replace(path)


def mark_disconnected():
    health_path().unlink(missing_ok=True)


def database_ready():
    import psycopg
    params = database_parameters()
    params.update(connect_timeout=3, options="-c statement_timeout=1000",
                  application_name="photoplatform-worker-health")
    try:
        with psycopg.connect(os.getenv("DATABASE_URL", ""), autocommit=True, **params) as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)
    except (psycopg.Error, OSError):
        return False


def healthy(probe=database_ready):
    try:
        timestamp = json.loads(health_path().read_text())["updated_at"]
        if type(timestamp) not in (int, float):
            return False
        age = time.time() - timestamp
        if not -5 <= age <= float(os.getenv("WORKER_HEALTH_MAX_AGE_SECONDS", "15")):
            return False
        return bool(probe())
    except (OSError, ValueError, KeyError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
