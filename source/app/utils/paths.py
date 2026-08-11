from pathlib import Path
import os

APP_NAME = "TelegramForwarder"

# Base directory for all writable data (AppData)
BASE_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME

DATA_DIR = BASE_DIR / "data"

SESSIONS_DIR = DATA_DIR / "sessions"
CACHE_DIR = DATA_DIR / "cache"
LOGS_DIR = DATA_DIR / "logs"
PROFILES_DIR = DATA_DIR / "profiles"
LOCKS_DIR = DATA_DIR / "locks"
DB_DIR = DATA_DIR / "database"
EXPORTS_DIR = DATA_DIR / "exports"

DESTINATIONS_CACHE = CACHE_DIR / "destinations.json"
DATABASE_FILE = DB_DIR / "telegram_forwarder.db"


def ensure_folders() -> None:
    for p in (
        DATA_DIR,
        SESSIONS_DIR,
        CACHE_DIR,
        LOGS_DIR,
        PROFILES_DIR,
        LOCKS_DIR,
        DB_DIR,
        EXPORTS_DIR,
    ):
        p.mkdir(parents=True, exist_ok=True)
