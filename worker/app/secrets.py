"""Resolve CSI-mounted values then replace this process with the selected command."""
import os
import sys

from .config import load_secret_files


def main():
    try:
        load_secret_files()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    command = sys.argv[1:] or [sys.executable, "-m", "app.consumer"]
    os.execvp(command[0], command)


if __name__ == "__main__":
    raise SystemExit(main())
