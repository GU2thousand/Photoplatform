"""ECS readiness: at least one recent, successful CloudWatch publication."""
import os
from collector import is_healthy


def main():
    try:
        interval = float(os.getenv("METRIC_INTERVAL_SECONDS", "60"))
        max_age = float(os.getenv("METRIC_HEALTH_MAX_AGE_SECONDS", str(max(180, 3 * interval))))
        return 0 if is_healthy(max_age=max_age) else 1
    except (ValueError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
