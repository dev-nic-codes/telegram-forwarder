from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class InstanceLock:
    path: Path

    def release(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        # On Windows, this works for checking if the process exists
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_lock(lock_path: Path, *, stale_after_sec: int = 12 * 60 * 60) -> InstanceLock:
    """
    Cross-platform single-instance lock using atomic create.
    If lock exists:
      - If PID in lock is still alive, refuse.
      - If PID is not alive or lock is stale, remove it and retry.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {"pid": os.getpid(), "created_at": int(time.time())}

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Read existing lock info
            try:
                raw = lock_path.read_text(encoding="utf-8").strip()
                info = json.loads(raw) if raw else {}
            except Exception:
                info = {}

            old_pid = int(info.get("pid", 0) or 0)
            created_at = int(info.get("created_at", 0) or 0)
            age = int(time.time()) - created_at if created_at > 0 else 10**9

            if old_pid and _pid_alive(old_pid):
                raise RuntimeError(f"Another instance is already running (pid={old_pid}).")

            # Not alive, treat as stale if old or malformed
            if age >= stale_after_sec:
                try:
                    lock_path.unlink()
                except Exception:
                    pass
                continue

            # If pid is dead but lock is "fresh", still safe to remove because pid is not alive
            try:
                lock_path.unlink()
            except Exception:
                pass
            continue

        try:
            os.write(fd, json.dumps(payload, indent=2).encode("utf-8"))
        finally:
            os.close(fd)

        return InstanceLock(path=lock_path)
