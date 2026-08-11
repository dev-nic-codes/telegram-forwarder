import sys
import os
from pathlib import Path


BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
sys.path.insert(0, str(BASE_DIR / "source"))

from app.service_runtime import main  # noqa: E402


if __name__ == "__main__":
    exit_code = int(main(sys.argv[1:]) or 0)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        # Service mode can leave third-party background internals alive after
        # our cleanup completes. Exit the process once the service coroutine
        # returns so systemd restarts/stops do not hang.
        os._exit(exit_code)
