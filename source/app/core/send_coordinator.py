from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

from app.utils.paths import PROFILES_DIR
from app.utils.storage import load_json, save_json


class DestinationLease:
    """Exclusive permission to send to one Telegram group."""

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock
        self._released = False

    async def __aenter__(self) -> "DestinationLease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class DestinationSendCoordinator:
    """Coordinates all campaigns sharing one Telegram user session.

    Telegram slow mode applies to the destination group, not to an individual
    campaign. A generic FloodWait is initially treated as destination-scoped
    because Telegram does not expose its scope. It is promoted to an account
    wait only when a second destination reports the same condition shortly
    afterwards. Keeping both here prevents concurrent campaigns from colliding
    and preserves waits across restarts.
    """

    FLOOD_CONFIRM_WINDOW_SECONDS = 120

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or (PROFILES_DIR / "send_cooldowns.json")
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._group_until: dict[int, float] = {}
        self._known_slowmode: dict[int, int] = {}
        self._flood_evidence: dict[int, dict[str, float]] = {}
        self._global_until = 0.0
        self._load()

    def _load(self) -> None:
        raw = load_json(self.state_path, default={})
        if not isinstance(raw, dict):
            return
        now = time.time()
        for key, value in (raw.get("group_until") or {}).items():
            try:
                group_id = int(key)
                deadline = float(value)
            except (TypeError, ValueError):
                continue
            if deadline > now:
                self._group_until[group_id] = deadline
        for key, value in (raw.get("known_slowmode") or {}).items():
            try:
                group_id = int(key)
                seconds = int(value)
            except (TypeError, ValueError):
                continue
            if seconds > 0:
                self._known_slowmode[group_id] = seconds
        for key, value in (raw.get("flood_evidence") or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                group_id = int(key)
                observed_at = float(value.get("observed_at") or 0)
                deadline = float(value.get("until") or 0)
            except (TypeError, ValueError):
                continue
            if (
                deadline > now
                and observed_at + self.FLOOD_CONFIRM_WINDOW_SECONDS > now
            ):
                self._flood_evidence[group_id] = {
                    "observed_at": observed_at,
                    "until": deadline,
                }
        try:
            global_until = float(raw.get("global_until") or 0)
        except (TypeError, ValueError):
            global_until = 0.0
        if global_until > now:
            self._global_until = global_until

    def _save(self) -> None:
        now = time.time()
        self._group_until = {
            group_id: deadline
            for group_id, deadline in self._group_until.items()
            if deadline > now
        }
        self._flood_evidence = {
            group_id: evidence
            for group_id, evidence in self._flood_evidence.items()
            if evidence.get("until", 0) > now
            and evidence.get("observed_at", 0) + self.FLOOD_CONFIRM_WINDOW_SECONDS > now
        }
        save_json(
            self.state_path,
            {
                "group_until": {str(k): v for k, v in self._group_until.items()},
                "known_slowmode": {str(k): v for k, v in self._known_slowmode.items()},
                "flood_evidence": {str(k): v for k, v in self._flood_evidence.items()},
                "global_until": self._global_until if self._global_until > now else 0,
            },
        )

    def is_busy(self, group_id: int) -> bool:
        lock = self._locks.get(int(group_id))
        return bool(lock and lock.locked())

    def ready_in_seconds(self, group_id: int) -> int:
        now = time.time()
        deadline = max(
            self._global_until,
            self._group_until.get(int(group_id), 0.0),
        )
        return max(0, int(math.ceil(deadline - now)))

    def is_ready(self, group_id: int) -> bool:
        return self.ready_in_seconds(group_id) == 0

    async def try_acquire(self, group_id: int) -> DestinationLease | None:
        group_id = int(group_id)
        async with self._guard:
            if not self.is_ready(group_id):
                return None
            lock = self._locks.setdefault(group_id, asyncio.Lock())
            if lock.locked():
                return None
            await lock.acquire()
            return DestinationLease(lock)

    def defer_group(self, group_id: int, seconds: int, *, remember_slowmode: bool) -> int:
        group_id = int(group_id)
        seconds = max(1, int(seconds))
        self._group_until[group_id] = max(
            self._group_until.get(group_id, 0.0),
            time.time() + seconds,
        )
        if remember_slowmode:
            self._known_slowmode[group_id] = max(
                self._known_slowmode.get(group_id, 0),
                seconds,
            )
        self._save()
        return seconds

    def record_success(self, group_id: int) -> int:
        group_id = int(group_id)
        seconds = int(self._known_slowmode.get(group_id, 0) or 0)
        if seconds > 0:
            self._group_until[group_id] = time.time() + seconds
            self._save()
        return seconds

    def defer_global(self, seconds: int) -> int:
        seconds = max(1, int(seconds))
        self._global_until = max(self._global_until, time.time() + seconds)
        self._save()
        return seconds

    def record_flood_wait(self, group_id: int, seconds: int) -> tuple[str, int]:
        """Record an ambiguous Telegram FloodWait without blocking all chats.

        The first signal quarantines only its destination. A second distinct
        destination within the confirmation window proves the wait is shared
        by the account and promotes it to a global cooldown.
        """

        group_id = int(group_id)
        seconds = max(1, int(seconds))
        now = time.time()
        deadline = now + seconds

        self._flood_evidence = {
            evidence_group: evidence
            for evidence_group, evidence in self._flood_evidence.items()
            if evidence.get("until", 0) > now
            and evidence.get("observed_at", 0) + self.FLOOD_CONFIRM_WINDOW_SECONDS > now
        }
        other_evidence = [
            evidence
            for evidence_group, evidence in self._flood_evidence.items()
            if evidence_group != group_id
        ]

        current = self._flood_evidence.get(group_id, {})
        self._flood_evidence[group_id] = {
            "observed_at": now,
            "until": max(float(current.get("until") or 0), deadline),
        }
        self._group_until[group_id] = max(
            self._group_until.get(group_id, 0.0),
            deadline,
        )

        if other_evidence:
            confirmed_until = max(
                deadline,
                *(float(evidence.get("until") or 0) for evidence in other_evidence),
            )
            self._global_until = max(self._global_until, confirmed_until)
            self._save()
            wait_seconds = max(1, int(math.ceil(self._global_until - now)))
            return "global", wait_seconds

        self._save()
        wait_seconds = max(1, int(math.ceil(self._group_until[group_id] - now)))
        return "destination", wait_seconds

    def snapshot(self) -> dict[str, Any]:
        return {
            "global_wait_seconds": max(0, int(math.ceil(self._global_until - time.time()))),
            "group_wait_seconds": {
                str(group_id): self.ready_in_seconds(group_id)
                for group_id in set(self._group_until) | set(self._known_slowmode)
            },
            "known_slowmode_seconds": dict(self._known_slowmode),
            "flood_evidence_groups": sorted(self._flood_evidence),
        }
