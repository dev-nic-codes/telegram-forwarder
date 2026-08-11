import json
import os
import threading
from pathlib import Path
from typing import Any, Callable


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def load_json(path: Path, default: Any) -> Any:
    with _path_lock(path):
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    with _path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def update_json(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    """Atomically read, update, and replace one JSON document."""
    with _path_lock(path):
        current = load_json(path, default)
        updated = updater(current)
        save_json(path, updated)
        return updated
