from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import shutil
import sqlite3
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from telethon.errors import SessionPasswordNeededError

from app.utils.paths import SESSIONS_DIR


@dataclass
class TgCredentials:
    api_id: int
    api_hash: str
    phone: str


def _safe_session_slug(phone: str) -> str:
    s = re.sub(r"\D+", "", phone or "")
    return s or "unknown"


def _is_malformed_session_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "database disk image is malformed" in text or "malformed" in text


def _restore_session_backup(session_path: Path) -> bool:
    backup_dir = session_path.parent / "backup-latest"
    if not backup_dir.exists():
        return False
    restored = False
    for source in backup_dir.iterdir():
        if not source.is_file():
            continue
        target = session_path.parent / source.name
        shutil.copy2(source, target)
        restored = True
    return restored


def _quarantine_corrupt_session(session_path: Path) -> None:
    for suffix in (".session", ".session-wal", ".session-shm"):
        source = Path(f"{session_path}{suffix}")
        if not source.exists():
            continue
        target = source.with_suffix(source.suffix + ".corrupt")
        try:
            if target.exists():
                target.unlink()
        except Exception:
            pass
        try:
            source.replace(target)
        except Exception:
            pass


class TgClient:
    def __init__(
        self,
        creds: TgCredentials,
        session_name: str | None = None,
        proxy: object | None = None,
    ) -> None:
        self._creds = creds

        # Always store sessions in data/sessions (consistent for EXE + source run).
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        slug = session_name or (
            f"telegram_forwarder_{_safe_session_slug(creds.phone)}_{creds.api_id}"
        )
        # Telethon will create "<path>.session" (SQLite) if no extension is provided.
        session_path = (Path(SESSIONS_DIR) / slug).resolve()

        self._session_path = session_path
        try:
            session = _PatchedSQLiteSession(str(session_path))
        except sqlite3.DatabaseError as exc:
            if not _is_malformed_session_error(exc):
                raise
            _quarantine_corrupt_session(session_path)
            if _restore_session_backup(session_path):
                session = _PatchedSQLiteSession(str(session_path))
            else:
                session = _PatchedSQLiteSession(str(session_path))
        self._client = TelegramClient(
            session,
            creds.api_id,
            creds.api_hash,
            proxy=proxy,
            receive_updates=False,
        )

    @property
    def client(self) -> TelegramClient:
        return self._client

    @property
    def session_path(self) -> Path:
        # Useful for debugging and support logs
        return self._session_path.with_suffix(".session")

    async def connect_and_login(self) -> None:
        # Defensive retry: if something briefly holds the SQLite session file,
        # retry a few times with a short backoff, then fail with a clear message.
        last_err: Exception | None = None
        for attempt in range(1, 6):
            try:
                await self._client.connect()
                last_err = None
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if "database is locked" in str(e).lower():
                    # Another instance is using the same session, or a previous run didn't exit cleanly.
                    await asyncio.sleep(0.4 * attempt)
                    continue
                raise
        if last_err is not None:
            raise RuntimeError(
                "Telegram session database is locked. "
                "Close any other running Telegram Forwarder EXE and try again. "
                f"Session file: {self.session_path}"
            ) from last_err

        if await self._client.is_user_authorized():
            return

        await self._client.send_code_request(self._creds.phone)
        code = input("Enter the Telegram code you received: ").strip()

        try:
            await self._client.sign_in(self._creds.phone, code)
        except SessionPasswordNeededError:
            pw = input("2FA password required. Enter your Telegram 2FA password: ").strip()
            await self._client.sign_in(password=pw)

    async def close(self) -> None:
        last_err: Exception | None = None
        for attempt in range(1, 6):
            try:
                await self._client.disconnect()
                return
            except sqlite3.OperationalError as e:
                last_err = e
                if "database is locked" in str(e).lower():
                    await asyncio.sleep(0.4 * attempt)
                    continue
                raise
        if last_err is not None:
            raise last_err


class _PatchedSQLiteSession(SQLiteSession):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        try:
            if self._conn is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
