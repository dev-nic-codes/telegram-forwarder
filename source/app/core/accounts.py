from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple
import sqlite3

from app.utils.paths import DATABASE_FILE


@dataclass
class Account:
    id: int
    label: str
    api_id: int
    api_hash: str
    phone: str
    proxy_type: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_user: Optional[str] = None
    proxy_pass: Optional[str] = None
    proxy_rotation_mode: Optional[str] = None
    proxy_rotation_on_login: Optional[bool] = None
    proxy_last_idx: Optional[int] = None
    daily_limit: Optional[int] = None
    warmup_profile: Optional[str] = None
    rate_multiplier: Optional[float] = None
    send_window_start: Optional[str] = None
    send_window_end: Optional[str] = None
    send_days: Optional[str] = None
    is_active: bool = False
    created_at: Optional[str] = None


@dataclass
class AccountProxy:
    id: int
    account_id: int
    label: str
    proxy_type: Optional[str]
    proxy_host: Optional[str]
    proxy_port: Optional[int]
    proxy_user: Optional[str]
    proxy_pass: Optional[str]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def _init_accounts_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            api_id INTEGER NOT NULL,
            api_hash TEXT NOT NULL,
            phone TEXT NOT NULL,
            proxy_type TEXT,
            proxy_host TEXT,
            proxy_port INTEGER,
            proxy_user TEXT,
            proxy_pass TEXT,
            proxy_rotation_mode TEXT,
            proxy_rotation_on_login INTEGER,
            proxy_last_idx INTEGER,
            daily_limit INTEGER,
            warmup_profile TEXT,
            rate_multiplier REAL,
            send_window_start TEXT,
            send_window_end TEXT,
            send_days TEXT,
            is_active INTEGER DEFAULT 0,
            created_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active)")
    conn.commit()

    # lightweight migration for new columns
    cur = conn.execute("PRAGMA table_info(accounts)")
    cols = {row[1] for row in cur.fetchall()}
    def _add_col(name: str, ddl: str) -> None:
        if name not in cols:
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {ddl}")
    _add_col("rate_multiplier", "rate_multiplier REAL")
    _add_col("send_window_start", "send_window_start TEXT")
    _add_col("send_window_end", "send_window_end TEXT")
    _add_col("send_days", "send_days TEXT")
    _add_col("proxy_rotation_mode", "proxy_rotation_mode TEXT")
    _add_col("proxy_rotation_on_login", "proxy_rotation_on_login INTEGER")
    _add_col("proxy_last_idx", "proxy_last_idx INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_proxies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            label TEXT,
            proxy_type TEXT,
            proxy_host TEXT,
            proxy_port INTEGER,
            proxy_user TEXT,
            proxy_pass TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_proxies_account ON account_proxies(account_id)")
    conn.commit()


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=int(row["id"]),
        label=str(row["label"] or ""),
        api_id=int(row["api_id"]),
        api_hash=str(row["api_hash"]),
        phone=str(row["phone"]),
        proxy_type=row["proxy_type"],
        proxy_host=row["proxy_host"],
        proxy_port=row["proxy_port"],
        proxy_user=row["proxy_user"],
        proxy_pass=row["proxy_pass"],
        proxy_rotation_mode=row["proxy_rotation_mode"],
        proxy_rotation_on_login=bool(row["proxy_rotation_on_login"]) if row["proxy_rotation_on_login"] is not None else None,
        proxy_last_idx=row["proxy_last_idx"],
        daily_limit=row["daily_limit"],
        warmup_profile=row["warmup_profile"],
        rate_multiplier=row["rate_multiplier"],
        send_window_start=row["send_window_start"],
        send_window_end=row["send_window_end"],
        send_days=row["send_days"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def _row_to_proxy(row: sqlite3.Row) -> AccountProxy:
    return AccountProxy(
        id=int(row["id"]),
        account_id=int(row["account_id"]),
        label=str(row["label"] or ""),
        proxy_type=row["proxy_type"],
        proxy_host=row["proxy_host"],
        proxy_port=row["proxy_port"],
        proxy_user=row["proxy_user"],
        proxy_pass=row["proxy_pass"],
    )


def list_accounts() -> List[Account]:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("SELECT * FROM accounts ORDER BY id ASC")
        rows = cur.fetchall()
        return [_row_to_account(r) for r in rows]
    finally:
        conn.close()


def add_account(*, label: str, api_id: int, api_hash: str, phone: str) -> int:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            """
            INSERT INTO accounts (label, api_id, api_hash, phone, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (label, api_id, api_hash, phone, datetime.now().isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def set_active_account(account_id: int) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        conn.execute("UPDATE accounts SET is_active = 0")
        cur = conn.execute("UPDATE accounts SET is_active = 1 WHERE id = ?", (account_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_active_account() -> Optional[Account]:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("SELECT * FROM accounts WHERE is_active = 1 LIMIT 1")
        row = cur.fetchone()
        return _row_to_account(row) if row else None
    finally:
        conn.close()


def delete_account(account_id: int) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_account_proxy(
    *,
    account_id: int,
    proxy_type: Optional[str],
    proxy_host: Optional[str],
    proxy_port: Optional[int],
    proxy_user: Optional[str],
    proxy_pass: Optional[str],
) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            """
            UPDATE accounts
            SET proxy_type = ?, proxy_host = ?, proxy_port = ?, proxy_user = ?, proxy_pass = ?
            WHERE id = ?
            """,
            (proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass, account_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_account_proxies(account_id: int) -> List[AccountProxy]:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            "SELECT * FROM account_proxies WHERE account_id = ? ORDER BY id ASC",
            (account_id,),
        )
        rows = cur.fetchall()
        return [_row_to_proxy(r) for r in rows]
    finally:
        conn.close()


def add_account_proxy(
    *,
    account_id: int,
    label: str,
    proxy_type: Optional[str],
    proxy_host: Optional[str],
    proxy_port: Optional[int],
    proxy_user: Optional[str],
    proxy_pass: Optional[str],
) -> int:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            """
            INSERT INTO account_proxies (
                account_id, label, proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (account_id, label, proxy_type, proxy_host, proxy_port, proxy_user, proxy_pass),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def delete_account_proxy(proxy_id: int) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("DELETE FROM account_proxies WHERE id = ?", (proxy_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_account_proxies(account_id: int) -> int:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("DELETE FROM account_proxies WHERE account_id = ?", (account_id,))
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def update_proxy_rotation_settings(
    *,
    account_id: int,
    mode: Optional[str],
    rotate_on_login: Optional[bool],
) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            """
            UPDATE accounts
            SET proxy_rotation_mode = ?, proxy_rotation_on_login = ?
            WHERE id = ?
            """,
            (mode, 1 if rotate_on_login else 0 if rotate_on_login is not None else None, account_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_account_advanced(
    *,
    account_id: int,
    rate_multiplier: Optional[float],
    send_window_start: Optional[str],
    send_window_end: Optional[str],
    send_days: Optional[str],
) -> bool:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute(
            """
            UPDATE accounts
            SET rate_multiplier = ?, send_window_start = ?, send_window_end = ?, send_days = ?
            WHERE id = ?
            """,
            (rate_multiplier, send_window_start, send_window_end, send_days, account_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_account(account_id: int) -> Optional[Account]:
    conn = _connect()
    try:
        _init_accounts_db(conn)
        cur = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
        return _row_to_account(row) if row else None
    finally:
        conn.close()


def telethon_proxy_tuple(account: Account) -> Optional[Tuple]:
    if not account.proxy_host or not account.proxy_port:
        return None
    try:
        import socks  # type: ignore
    except Exception:
        return None
    ptype = (account.proxy_type or "socks5").strip().lower()
    if ptype in ("socks5", "socks"):
        p = socks.SOCKS5
    elif ptype in ("socks4",):
        p = socks.SOCKS4
    elif ptype in ("http", "https"):
        p = socks.HTTP
    else:
        p = socks.SOCKS5
    return (p, account.proxy_host, int(account.proxy_port), True, account.proxy_user, account.proxy_pass)


def _proxy_tuple_from_parts(
    proxy_type: Optional[str],
    proxy_host: Optional[str],
    proxy_port: Optional[int],
    proxy_user: Optional[str],
    proxy_pass: Optional[str],
) -> Optional[Tuple]:
    if not proxy_host or not proxy_port:
        return None
    try:
        import socks  # type: ignore
    except Exception:
        return None
    ptype = (proxy_type or "socks5").strip().lower()
    if ptype in ("socks5", "socks"):
        p = socks.SOCKS5
    elif ptype in ("socks4",):
        p = socks.SOCKS4
    elif ptype in ("http", "https"):
        p = socks.HTTP
    else:
        p = socks.SOCKS5
    return (p, proxy_host, int(proxy_port), True, proxy_user, proxy_pass)


def pick_proxy_for_account(account: Account, *, rotate: bool = True) -> Optional[Tuple]:
    proxies = list_account_proxies(account.id)
    if not proxies:
        return telethon_proxy_tuple(account)

    mode = (account.proxy_rotation_mode or "round_robin").strip().lower()
    rotate_on_login = account.proxy_rotation_on_login
    if rotate_on_login is None:
        rotate_on_login = True
    idx = account.proxy_last_idx if account.proxy_last_idx is not None else -1

    if not rotate or not rotate_on_login:
        pick_idx = idx if 0 <= idx < len(proxies) else 0
    elif mode == "random":
        import random
        pick_idx = random.randint(0, len(proxies) - 1)
    else:
        pick_idx = (idx + 1) % len(proxies)

    proxy = proxies[pick_idx]
    conn = _connect()
    try:
        _init_accounts_db(conn)
        conn.execute(
            "UPDATE accounts SET proxy_last_idx = ? WHERE id = ?",
            (int(pick_idx), int(account.id)),
        )
        conn.commit()
    finally:
        conn.close()

    return _proxy_tuple_from_parts(
        proxy.proxy_type,
        proxy.proxy_host,
        proxy.proxy_port,
        proxy.proxy_user,
        proxy.proxy_pass,
    )
