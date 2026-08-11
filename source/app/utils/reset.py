from __future__ import annotations

from pathlib import Path

from app.utils.paths import DATA_DIR, PROFILES_DIR, SESSIONS_DIR, DESTINATIONS_CACHE

CONFIG_PATH = DATA_DIR / "config.json"
TARGETS_PATH = PROFILES_DIR / "destination_targets.json"
CAMPAIGNS_PATH = PROFILES_DIR / "campaigns.json"


def _safe_unlink(p: Path) -> bool:
    try:
        if p.exists():
            p.unlink()
            return True
    except Exception:
        return False
    return False


def _safe_rmtree(dir_path: Path) -> int:
    if not dir_path.exists():
        return 0
    deleted = 0
    for p in dir_path.rglob("*"):
        if p.is_file():
            if _safe_unlink(p):
                deleted += 1
    # remove empty dirs bottom-up
    for p in sorted([x for x in dir_path.rglob("*") if x.is_dir()], key=lambda x: len(str(x)), reverse=True):
        try:
            p.rmdir()
        except Exception:
            pass
    try:
        dir_path.rmdir()
    except Exception:
        pass
    return deleted


def delete_config() -> bool:
    return _safe_unlink(CONFIG_PATH)


def delete_destinations_cache() -> bool:
    return _safe_unlink(DESTINATIONS_CACHE)


def delete_targets() -> bool:
    return _safe_unlink(TARGETS_PATH)


def delete_campaigns() -> bool:
    return _safe_unlink(CAMPAIGNS_PATH)


def delete_sessions() -> int:
    return _safe_rmtree(SESSIONS_DIR)


def nuke_all() -> dict:
    return {
        "config": delete_config(),
        "dest_cache": delete_destinations_cache(),
        "targets": delete_targets(),
        "campaigns": delete_campaigns(),
        "session_files_deleted": delete_sessions(),
    }
