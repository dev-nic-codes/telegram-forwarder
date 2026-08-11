from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from telethon.tl.types import Channel, Chat, User

from app.alerts.telegram_bot import BotControl, TelegramBotManager, load_bot_config
from app.analytics.history_tracker import get_history
from app.core.accounts import (
    get_active_account,
    list_account_proxies,
    list_accounts,
    pick_proxy_for_account,
)
from app.core.campaigns import Campaign, get_campaign, list_campaigns, replace_campaign
from app.core.group_sync import (
    GroupSyncReport,
    set_group_enabled,
    set_group_topic_enabled,
    sync_sendable_groups,
)
from app.core.runner import run_campaign
from app.core.send_coordinator import DestinationSendCoordinator
from app.core.sources import (
    add_source,
    normalize_source_ref,
    remove_source,
    resolve_source_entity,
    source_ref_from_dialog,
)
from app.core.telegram_client import TgClient, TgCredentials
from app.main import (
    _estimate_send_rates,
    _format_disabled_targets,
    _safe_parse_next_at,
    _status_callback_factory,
)
from app.utils.config import load_config
from app.utils.lock import InstanceLock, acquire_lock
from app.utils.paths import EXPORTS_DIR, LOCKS_DIR, PROFILES_DIR, ensure_folders
from app.utils.safety import assess_campaign_risk
from app.utils.settings import AdvancedSettings, load_settings, save_settings, update_last_export
from app.utils.state import load_state, save_state, stop_state


@dataclass
class ServiceOptions:
    dry_run: bool = False
    allow_risky: bool = False
    skip_bot: bool = False
    start_all: bool = False


def _friendly_source_setup_error(exc: Exception) -> str:
    text = f"{type(exc).__name__} {exc}".casefold()
    if "private" in text or "forbidden" in text:
        return "the logged-in account cannot access this chat"
    if "username" in text and ("invalid" in text or "not occupied" in text):
        return "the Telegram username does not exist"
    if "entity" in text or "not found" in text:
        return "Telegram could not find this chat"
    if "timeout" in text or "connection" in text:
        return "Telegram connection failed; try again"
    if isinstance(exc, ValueError):
        return str(exc)
    return "the source could not be validated"


class ForwarderService:
    def __init__(self, options: ServiceOptions) -> None:
        self.options = options
        self.settings: AdvancedSettings = load_settings()
        self.tg: TgClient | None = None
        self.bot_mgr: TelegramBotManager | None = None
        self.running_tasks: Dict[str, Dict[str, Any]] = {}
        self.shutdown_event: asyncio.Event | None = None
        self.app_lock: InstanceLock | None = None
        self.bot_lock: InstanceLock | None = None
        self.main_loop: asyncio.AbstractEventLoop | None = None
        self.recent_logs: deque[str] = deque(maxlen=500)
        self.send_coordinator = DestinationSendCoordinator()

    async def run(self) -> int:
        ensure_folders()
        save_settings(self.settings)
        self.main_loop = asyncio.get_running_loop()
        self.shutdown_event = asyncio.Event()
        self._record_runtime("Service booting.")
        self._configure_loop()
        self._install_signal_handlers()
        self.app_lock = acquire_lock(LOCKS_DIR / "telegram_forwarder.lock")

        maintenance_tasks: list[asyncio.Task] = []

        try:
            await self._connect_account()
            try:
                await self._sync_groups_once(restart_changed=False, reset_all=False)
            except Exception as exc:
                self._record_runtime(f"Initial group sync failed: {type(exc).__name__}: {exc}")
            try:
                await self._import_existing_campaign_sources()
            except Exception as exc:
                self._record_runtime(f"Existing source import failed: {type(exc).__name__}: {exc}")
            maintenance_tasks = [
                asyncio.create_task(self._cleanup_history_loop()),
                asyncio.create_task(self._auto_export_loop()),
                asyncio.create_task(self._connection_watchdog_loop()),
                asyncio.create_task(self._group_sync_loop()),
            ]
            await self._start_bot()
            await self._autostart_campaigns()
            assert self.shutdown_event is not None
            await self.shutdown_event.wait()
            return 0
        finally:
            self._record_runtime("Service shutting down.")
            for task in maintenance_tasks:
                task.cancel()
            if maintenance_tasks:
                await asyncio.gather(*maintenance_tasks, return_exceptions=True)
            await self._shutdown()

    async def _await_with_timeout(self, awaitable, *, timeout: float, label: str) -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError:
            self._record_runtime(f"{label} timed out after {timeout:.0f}s; continuing shutdown.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_runtime(f"{label} failed during shutdown: {type(exc).__name__}: {exc}")

    def _record_runtime(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.recent_logs.append(line)
        print(message)

    def refresh_settings(self) -> None:
        self.settings = load_settings()
        self._record_runtime("Reloaded advanced settings from disk.")

    def _configure_loop(self) -> None:
        loop = asyncio.get_running_loop()

        def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
            exc = context.get("exception")
            msg = str(exc) if exc else context.get("message", "")
            if "NoneType" in msg and "shutdown" in msg:
                return
            loop.default_exception_handler(context)

        try:
            loop.set_exception_handler(_loop_exception_handler)
        except Exception:
            pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            try:
                sig = getattr(__import__("signal"), signame)
                if self.shutdown_event is not None:
                    loop.add_signal_handler(sig, self.shutdown_event.set)
            except Exception:
                continue

    async def _connect_account(self) -> None:
        accounts = list_accounts()
        if accounts:
            active = get_active_account()
            if active is None:
                raise RuntimeError(
                    "No active account is selected. Set one once in the interactive app before enabling service mode."
                )
            proxy = pick_proxy_for_account(active, rotate=True)
            if proxy is None:
                has_pool = len(list_account_proxies(active.id)) > 0
                if has_pool or (active.proxy_host and active.proxy_port):
                    print("Proxy is configured but unavailable; continuing without proxy.")
            creds = TgCredentials(api_id=active.api_id, api_hash=active.api_hash, phone=active.phone)
            self.tg = TgClient(creds, session_name=f"acct_{active.id}", proxy=proxy)
        else:
            cfg = load_config()
            if not cfg:
                raise RuntimeError("No config found. Run the interactive setup once before enabling service mode.")
            self.tg = TgClient(TgCredentials(api_id=cfg.api_id, api_hash=cfg.api_hash, phone=cfg.phone))

        last_err: Exception | None = None
        for attempt in range(1, 6):
            try:
                await self.tg.client.connect()
                last_err = None
                break
            except sqlite3.OperationalError as exc:
                last_err = exc
                if "database is locked" in str(exc).lower():
                    await asyncio.sleep(0.4 * attempt)
                    continue
                raise
            except sqlite3.DatabaseError as exc:
                last_err = exc
                if "malformed" in str(exc).lower():
                    restored = self._restore_session_backup()
                    if restored:
                        await asyncio.sleep(0.2 * attempt)
                        continue
                raise

        if last_err is not None:
            raise RuntimeError(
                "Telegram session database is locked. Stop any other Telegram Forwarder instance and retry."
            ) from last_err

        if not await self.tg.client.is_user_authorized():
            restored = self._restore_session_backup()
            if restored:
                try:
                    await self.tg.client.disconnect()
                except Exception:
                    pass
                await self.tg.client.connect()
            if not await self.tg.client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authorized. Copy the existing session to the server or log in once interactively."
                )
            self._record_runtime("Restored Telegram session from backup.")

        me = await self.tg.client.get_me()
        expected_user_id = os.environ.get("TELEGRAM_EXPECTED_USER_ID", "").strip()
        if expected_user_id and str(getattr(me, "id", "")) != expected_user_id:
            raise RuntimeError(
                "Connected Telegram account does not match TELEGRAM_EXPECTED_USER_ID. "
                f"Expected {expected_user_id}, got {getattr(me, 'id', '-')}. "
                "Refusing to start campaigns."
            )
        username = getattr(me, "username", None) or "-"
        self._record_runtime(
            f"Telegram account connected: @{username} (id={getattr(me, 'id', '-')}). "
            f"Session: {self.tg.session_path}"
        )
        self._backup_session_files()

    async def _ensure_account_connected(self, *, record: bool = False) -> None:
        if self.tg is None:
            raise RuntimeError("Telegram account is not configured.")
        try:
            if self.tg.client.is_connected():
                return
        except Exception:
            pass
        if record:
            self._record_runtime("Telegram client disconnected. Attempting reconnect.")
        try:
            await self.tg.client.connect()
        except sqlite3.DatabaseError as exc:
            if "malformed" in str(exc).lower() and self._restore_session_backup():
                try:
                    await self.tg.client.disconnect()
                except Exception:
                    pass
                await self.tg.client.connect()
            else:
                raise
        except Exception:
            try:
                await self.tg.client.disconnect()
            except Exception:
                pass
            await self.tg.client.connect()
        if not await self.tg.client.is_user_authorized():
            restored = self._restore_session_backup()
            if restored:
                try:
                    await self.tg.client.disconnect()
                except Exception:
                    pass
                await self.tg.client.connect()
        if not await self.tg.client.is_user_authorized():
            raise RuntimeError("Telegram session is no longer authorized. Log in again once interactively.")
        if record:
            self._record_runtime("Telegram client reconnected.")

    async def _connection_watchdog_loop(self) -> None:
        while True:
            try:
                if self.tg is not None:
                    await self._ensure_account_connected(record=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_runtime(f"Reconnect watchdog failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(30)

    def _reset_state_after_group_sync(self, campaign_id: str) -> None:
        state_path = PROFILES_DIR / f"state_{campaign_id}.json"
        state = load_state(state_path, campaign_id)
        if state is None:
            return
        state.paused = False
        state.paused_at = None
        state.paused_reason = None
        state.stopped = False
        state.stopped_at = None
        state.error_streak = 0
        state.target_fail_counts = {}
        state.target_disabled = {}
        state.next_at = datetime.now()
        save_state(state_path, state)

    async def _restart_campaigns_after_group_sync(self, campaign_ids: list[str]) -> None:
        for campaign_id in campaign_ids:
            info = self.running_tasks.get(campaign_id)
            was_running = info is not None
            dry = bool(info.get("dry")) if info else self.options.dry_run
            if info is not None:
                task = info.get("task")
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            self._reset_state_after_group_sync(campaign_id)
            campaign = get_campaign(campaign_id)
            if not was_running or campaign is None or not campaign.target_refs:
                continue
            ok, message = await self._start_campaign_background(
                campaign,
                dry=dry,
                allow_risky=self.options.allow_risky,
                notify_start=False,
            )
            if not ok:
                self._record_runtime(
                    f"Could not restart refreshed campaign {campaign.name} ({campaign.id}): {message}"
                )

    async def _sync_groups_once(
        self,
        *,
        restart_changed: bool,
        reset_all: bool,
        refresh_topics: bool = False,
    ) -> GroupSyncReport:
        if self.tg is None:
            raise RuntimeError("Telegram account is not connected.")
        await self._ensure_account_connected(record=False)
        report = await sync_sendable_groups(self.tg.client, refresh_topics=refresh_topics)
        affected = (
            [campaign.id for campaign in list_campaigns() if campaign.target_refs]
            if reset_all
            else list(report.restart_campaign_ids)
        )
        if restart_changed:
            await self._restart_campaigns_after_group_sync(affected)
        else:
            for campaign_id in affected:
                self._reset_state_after_group_sync(campaign_id)
        self._record_runtime(
            "Group sync complete: "
            f"sendable={report.sendable_groups}, selected={report.selected_groups}, "
            f"excluded={report.excluded_groups}, added={report.added_groups}, "
            f"removed={report.removed_targets}, campaigns_updated={report.campaigns_updated}."
        )
        return report

    async def _group_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(1800)
            try:
                await self._sync_groups_once(restart_changed=True, reset_all=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_runtime(f"Scheduled group sync failed: {type(exc).__name__}: {exc}")

    def _backup_session_dir(self) -> Path:
        assert self.tg is not None
        return self.tg.session_path.parent / "backup-latest"

    def _backup_session_files(self) -> None:
        if self.tg is None:
            return
        session_file = self.tg.session_path
        backup_dir = self._backup_session_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{session_file}{suffix}")
            if not source.exists():
                continue
            target = backup_dir / source.name
            shutil.copy2(source, target)

    def _restore_session_backup(self) -> bool:
        if self.tg is None:
            return False
        backup_dir = self._backup_session_dir()
        if not backup_dir.exists():
            return False
        restored = False
        for source in backup_dir.iterdir():
            if not source.is_file():
                continue
            target = self.tg.session_path.parent / source.name
            shutil.copy2(source, target)
            restored = True
        return restored

    async def _start_bot(self) -> None:
        if self.options.skip_bot:
            self._record_runtime("Bot startup skipped by option.")
            return

        cfg = load_bot_config()
        if not cfg or not cfg.get("enabled", False):
            self._record_runtime("Telegram bot is not enabled.")
            return

        if not cfg.get("auto_start_on_login", True):
            self._record_runtime("Telegram bot is enabled but auto-start is disabled.")
            return

        self.bot_mgr = TelegramBotManager(cfg["token"], cfg["chat_id"], control=self._build_bot_control())
        self.bot_lock = acquire_lock(LOCKS_DIR / "telegram_forwarder_bot.lock")
        self.bot_mgr.start_background()
        self._record_runtime("Telegram control bot started.")

    async def _autostart_campaigns(self) -> None:
        started = 0
        skipped = 0

        for campaign in list_campaigns():
            if not self.options.start_all and not getattr(campaign, "enabled", True):
                continue
            risk = assess_campaign_risk(campaign)
            if (risk.guardrails or risk.level == "high") and not self.options.allow_risky:
                skipped += 1
                self._record_runtime(
                    f"Skipped campaign {campaign.name} ({campaign.id}) because risk checks require confirmation. "
                    "Re-run with --allow-risky if this is intentional."
                )
                continue

            self._normalize_state_for_autostart(campaign)
            ok, msg = await self._start_campaign_background(
                campaign,
                dry=self.options.dry_run,
                allow_risky=self.options.allow_risky,
                notify_start=False,
            )
            if ok:
                started += 1
                self._record_runtime(f"Started campaign {campaign.name} ({campaign.id}).")
            else:
                skipped += 1
                self._record_runtime(f"Did not start campaign {campaign.name} ({campaign.id}): {msg}")

        mode = "all saved campaigns" if self.options.start_all else "enabled campaigns"
        self._record_runtime(f"Autostart complete for {mode}. Started={started}, skipped={skipped}.")

    def _normalize_state_for_autostart(self, campaign: Campaign) -> None:
        state_path = PROFILES_DIR / f"state_{campaign.id}.json"
        state = load_state(state_path, campaign.id)
        if state is None:
            return
        state.paused = False
        state.paused_at = None
        state.paused_reason = None
        state.stopped = False
        state.stopped_at = None
        save_state(state_path, state)

    def _current_account_runtime(self) -> tuple[float, Optional[Dict[str, Any]]]:
        account_rate = 1.0
        account_schedule = None
        active = get_active_account()
        if active is None:
            return account_rate, account_schedule

        account_rate = float(getattr(active, "rate_multiplier", None) or self.settings.account_rate_multiplier_default)
        if getattr(active, "send_window_start", None) and getattr(active, "send_window_end", None):
            account_schedule = {
                "days_mode": getattr(active, "send_days", "all") or "all",
                "windows": None,
                "sleep_start": getattr(active, "send_window_start"),
                "sleep_end": getattr(active, "send_window_end"),
            }
        return account_rate, account_schedule

    async def _start_campaign_background(
        self,
        campaign: Campaign,
        *,
        dry: bool,
        allow_risky: bool,
        notify_start: bool = True,
    ) -> tuple[bool, str]:
        if self.tg is None:
            return False, "Not logged in."
        if campaign.id in self.running_tasks:
            return False, "Campaign already running."

        event_buf = deque(maxlen=200)
        task = asyncio.create_task(
            self._run_campaign_task(
                campaign=campaign,
                dry=dry,
                allow_risky=allow_risky,
                event_buf=event_buf,
                notify_start=notify_start,
            )
        )
        self.running_tasks[campaign.id] = {
            "task": task,
            "name": campaign.name,
            "dry": dry,
            "started_at": datetime.now(),
            "events": event_buf,
        }
        return True, "started"

    async def _run_campaign_task(
        self,
        *,
        campaign: Campaign,
        dry: bool,
        allow_risky: bool,
        event_buf: deque[str],
        notify_start: bool,
    ) -> None:
        state_path = PROFILES_DIR / f"state_{campaign.id}.json"
        bot_cfg = load_bot_config()
        if not bot_cfg or not bot_cfg.get("enabled", False):
            bot_cfg = None
        def _event_sink(message: str) -> None:
            clean = str(message).strip()
            if not clean:
                return
            tagged = f"{campaign.name}: {clean}"
            event_buf.append(tagged)
            self.recent_logs.append(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {tagged}")
        _, on_event = _status_callback_factory(
            campaign,
            dry=dry,
            bot_mgr=self.bot_mgr,
            bot_cfg=bot_cfg,
            settings=self.settings,
            suppress_updates_fn=lambda: True,
            event_sink=_event_sink,
            notify_start=notify_start,
        )
        account_rate, account_schedule = self._current_account_runtime()

        try:
            self._record_runtime(f"Campaign task started: {campaign.name} ({campaign.id})")
            await run_campaign(
                tg_client=self.tg.client,
                campaign=campaign,
                state_path=state_path,
                dry_run=dry,
                seed=None,
                on_event=on_event,
                settings=self.settings,
                account_rate_multiplier=account_rate,
                account_schedule=account_schedule,
                reconnect_minutes=getattr(self.settings, "force_reconnect_minutes", None),
                send_coordinator=self.send_coordinator,
            )
        except asyncio.CancelledError:
            self._record_runtime(f"Campaign task cancelled: {campaign.name} ({campaign.id})")
            raise
        except Exception as exc:
            self._record_runtime(
                f"Campaign runner failed for {campaign.name} ({campaign.id}): {type(exc).__name__}: {exc}"
            )
        finally:
            self.running_tasks.pop(campaign.id, None)
            self._record_runtime(f"Campaign task stopped: {campaign.name} ({campaign.id})")

    async def _shutdown(self) -> None:
        self._record_runtime("Stopping campaign tasks.")
        for campaign_id, info in list(self.running_tasks.items()):
            task = info.get("task")
            if task:
                task.cancel()
        campaign_tasks = [info.get("task") for info in list(self.running_tasks.values()) if info.get("task")]
        if campaign_tasks:
            await self._await_with_timeout(
                asyncio.gather(*campaign_tasks, return_exceptions=True),
                timeout=20,
                label="Campaign task shutdown",
            )
        self.running_tasks.clear()

        if self.bot_mgr is not None:
            self._record_runtime("Stopping Telegram control bot.")
            try:
                await self._await_with_timeout(
                    asyncio.to_thread(self.bot_mgr.stop_background, 5),
                    timeout=8,
                    label="Telegram control bot shutdown",
                )
            except RuntimeError:
                # asyncio.to_thread can be unavailable on very old Python versions.
                self.bot_mgr.stop_background(timeout_sec=5)
            self.bot_mgr = None

        if self.bot_lock is not None:
            try:
                self.bot_lock.release()
            except Exception:
                pass
            self.bot_lock = None

        if self.tg is not None:
            self._record_runtime("Backing up and closing Telegram user session.")
            try:
                self._backup_session_files()
            except Exception:
                pass
            await self._await_with_timeout(
                self.tg.close(),
                timeout=10,
                label="Telegram user session close",
            )
            self.tg = None

        if self.app_lock is not None:
            try:
                self.app_lock.release()
            except Exception:
                pass
            self.app_lock = None
        self._record_runtime("Service shutdown complete.")

    async def _in_main(self, coro):
        current = asyncio.get_running_loop()
        if self.main_loop is None or current is self.main_loop:
            return await coro
        future = asyncio.run_coroutine_threadsafe(coro, self.main_loop)
        return await asyncio.wrap_future(future)

    async def _cleanup_history_loop(self) -> None:
        retention_days = getattr(self.settings, "history_retention_days", None)
        if retention_days is None or retention_days <= 0:
            return
        while True:
            try:
                history = await get_history()
                await history.cleanup_older_than(retention_days)
            except Exception:
                pass
            await asyncio.sleep(3600)

    async def _auto_export_loop(self) -> None:
        hours = getattr(self.settings, "auto_export_hours", None)
        if hours is None or hours <= 0:
            return
        while True:
            try:
                last = getattr(self.settings, "last_auto_export_at", None)
                last_dt = datetime.fromisoformat(last) if last else None
            except Exception:
                last_dt = None

            now = datetime.now()
            if last_dt is None or (now - last_dt) >= timedelta(hours=int(hours)):
                try:
                    history = await get_history()
                    records = await history.get_recent(limit=1000)
                    if records:
                        ts = now.strftime("%Y%m%d_%H%M%S")
                        filepath = EXPORTS_DIR / f"message_history_auto_{ts}.csv"
                        with open(filepath, "w", encoding="utf-8") as handle:
                            handle.write(
                                "Timestamp,Ad ID,Ad Name,Message Link,Group ID,Group Title,Topic ID,Topic Title,"
                                "Success,Error Type,Error Message,Stars Cost\n"
                            )
                            for row in records:
                                handle.write(f'"{row["timestamp"]}",')
                                handle.write(f'"{row["ad_id"]}",')
                                handle.write(f'"{row["campaign_name"]}",')
                                handle.write(f'"{row["message_link"]}",')
                                handle.write(f'"{row["group_id"]}",')
                                handle.write(f'"{row["group_title"]}",')
                                handle.write(f'"{row.get("topic_id", "")}",')
                                handle.write(f'"{row.get("topic_title", "")}",')
                                handle.write(f'"{row["success"]}",')
                                handle.write(f'"{row.get("error_type", "")}",')
                                handle.write(f'"{row.get("error_message", "")}",')
                                handle.write(f'"{row.get("stars_cost", 0)}"\n')
                        update_last_export(self.settings, now)
                except Exception:
                    pass
            await asyncio.sleep(60)

    def _build_bot_control(self) -> BotControl:
        return BotControl(
            list_running=self.ctrl_list_running,
            stop_running=self.ctrl_stop_running,
            start_ad=self.ctrl_start_ad,
            resume_schedule=self.ctrl_resume_schedule,
            ad_status=self.ctrl_ad_status,
            enable_ad=self.ctrl_enable_ad,
            disable_ad=self.ctrl_disable_ad,
            health=self.ctrl_health,
            list_disabled=self.ctrl_list_disabled,
            clear_disabled=self.ctrl_clear_disabled,
            dashboard_status=self.ctrl_dashboard_status,
            recent_logs=self.ctrl_recent_logs,
            reload_settings=self.refresh_settings,
            scan_groups=self.ctrl_scan_groups,
            set_group_enabled=self.ctrl_set_group_enabled,
            set_group_topic_enabled=self.ctrl_set_group_topic_enabled,
            add_source=self.ctrl_add_source,
            remove_source=self.ctrl_remove_source,
            reload_ad=self.ctrl_reload_ad,
        )

    async def ctrl_reload_ad(self, ad_id: str) -> str:
        async def _impl() -> str:
            info = self.running_tasks.get(ad_id)
            if info is None:
                return "Saved. The change will apply when this ad starts."

            dry = bool(info.get("dry", False))
            task = info.get("task")
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.running_tasks.pop(ad_id, None)

            campaign = get_campaign(ad_id)
            if campaign is None:
                return "Saved, but the ad could not be reloaded because it no longer exists."
            if not getattr(campaign, "enabled", True):
                return "Saved. The ad is disabled, so it was not restarted."

            ok, message = await self._start_campaign_background(
                campaign,
                dry=dry,
                allow_risky=self.options.allow_risky,
                notify_start=False,
            )
            if not ok:
                return f"Saved, but the running ad could not reload: {message}"
            return "Saved and applied to the running ad."

        return await self._in_main(_impl())

    async def ctrl_scan_groups(self) -> str:
        async def _impl() -> str:
            report = await self._sync_groups_once(
                restart_changed=True,
                reset_all=False,
                refresh_topics=True,
            )
            return (
                f"Scan complete: {report.sendable_groups} sendable groups found, "
                f"{report.selected_groups} selected and {report.excluded_groups} excluded. "
                f"{report.added_groups} added, {report.removed_targets} stale targets removed, "
                f"{report.campaigns_updated} ads refreshed. "
                f"{report.topics_cached} forum topics found across {report.forum_groups} forum groups."
            )

        return await self._in_main(_impl())

    async def ctrl_set_group_topic_enabled(self, group_id: int, topic_id: int, enabled: bool) -> str:
        async def _impl() -> str:
            changed = set_group_topic_enabled(int(group_id), int(topic_id), bool(enabled))
            await self._sync_groups_once(restart_changed=False, reset_all=False)
            state = "selected" if enabled else "removed"
            suffix = "" if changed else " (already set)"
            return f"Topic {state}{suffix}. New ads can now use the updated topic selection."

        return await self._in_main(_impl())

    async def ctrl_set_group_enabled(self, group_id: int, enabled: bool) -> str:
        async def _impl() -> str:
            changed = set_group_enabled(int(group_id), bool(enabled))
            report = await self._sync_groups_once(restart_changed=True, reset_all=False)
            state = "included" if enabled else "excluded"
            suffix = "" if changed else " (already set)"
            return (
                f"Group {state}{suffix}. "
                f"{report.selected_groups} of {report.sendable_groups} groups are selected."
            )

        return await self._in_main(_impl())

    async def _import_existing_campaign_sources(self) -> None:
        refs: list[str] = ["me"]
        seen: set[str] = {"me"}
        for campaign in list_campaigns():
            for raw in list(getattr(campaign, "latest_sources", None) or []):
                value = str(raw or "").strip()
                key = value.casefold()
                if not value or key in seen:
                    continue
                seen.add(key)
                refs.append(value)
        if not refs:
            return
        result = await self.ctrl_add_source("\n".join(refs))
        self._record_runtime(f"Existing campaign sources synchronized: {result}")

    async def ctrl_add_source(self, raw: str) -> str:
        async def _impl() -> str:
            if self.tg is None:
                raise RuntimeError("Telegram account is not connected.")
            await self._ensure_account_connected(record=False)
            values = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
            if not values:
                raise ValueError("Send at least one source.")

            added_labels: list[str] = []
            updated_labels: list[str] = []
            errors: list[str] = []
            for value in values:
                try:
                    normalized = normalize_source_ref(value)
                    peer = int(normalized) if normalized.lstrip("-").isdigit() else normalized
                    entity = await resolve_source_entity(self.tg.client, peer)
                    if normalized != "me" and not isinstance(entity, (Channel, Chat, User)):
                        raise ValueError("The source must be a readable Telegram chat or Saved Messages.")
                    await self.tg.client.get_messages(entity, limit=1)
                    username = str(getattr(entity, "username", "") or "").strip()
                    if normalized == "me":
                        canonical = "me"
                        label = "Saved Messages"
                        kind = "saved"
                    elif isinstance(entity, User):
                        canonical = f"@{username}" if username else str(int(entity.id))
                        first_name = str(getattr(entity, "first_name", "") or "").strip()
                        last_name = str(getattr(entity, "last_name", "") or "").strip()
                        label = " ".join(part for part in (first_name, last_name) if part) or canonical
                        kind = "private"
                    elif isinstance(entity, Channel):
                        canonical = source_ref_from_dialog(int(entity.id), "channel")
                        label = str(getattr(entity, "title", None) or canonical)
                        kind = "group" if bool(getattr(entity, "megagroup", False)) else "channel"
                    else:
                        canonical = source_ref_from_dialog(int(entity.id), "chat")
                        label = str(getattr(entity, "title", None) or canonical)
                        kind = "group"
                    source, created = add_source(ref=canonical, label=label, kind=kind)
                    (added_labels if created else updated_labels).append(source.label)
                except Exception as exc:
                    errors.append(f"{value}: {_friendly_source_setup_error(exc)}")

            if not added_labels and not updated_labels:
                raise ValueError("No source could be added. " + "; ".join(errors[:3]))
            parts: list[str] = []
            if added_labels:
                parts.append(f"Added {len(added_labels)} source(s): {', '.join(added_labels)}")
            if updated_labels:
                parts.append(f"Updated {len(updated_labels)} existing source(s).")
            if errors:
                parts.append(f"Skipped {len(errors)} invalid or inaccessible source(s).")
            return " ".join(parts)

        return await self._in_main(_impl())

    async def ctrl_remove_source(self, index: int) -> str:
        async def _impl() -> str:
            source = remove_source(int(index))
            if source is None:
                return "Source not found."
            return f"Removed {source.label}."

        return await self._in_main(_impl())

    async def ctrl_recent_logs(self, limit: int) -> str:
        async def _impl() -> str:
            try:
                limit_value = max(1, min(50, int(limit)))
            except Exception:
                limit_value = 15
            items = list(self.recent_logs)[-limit_value:]
            lines = ["🧾 Recent runtime logs"]
            if not items:
                lines.extend(["", "No runtime logs have been captured yet."])
                return "\n".join(lines)
            lines.append("")
            lines.extend(items)
            return "\n".join(lines)

        return await self._in_main(_impl())

    async def ctrl_list_running(self) -> str:
        async def _impl() -> str:
            if not self.running_tasks:
                return "Running ads\n\nNo ads are running."

            lines = ["Running ads"]
            for campaign_id, info in self.running_tasks.items():
                mode = "DRY" if info.get("dry") else "LIVE"
                started_at = info.get("started_at")
                started_txt = started_at.strftime("%H:%M:%S %d/%m/%Y") if isinstance(started_at, datetime) else "-"
                lines.append(f"- {info.get('name')} | ID: {campaign_id} | Mode: {mode} | Started: {started_txt}")
            return "\n".join(lines)

        return await self._in_main(_impl())

    async def ctrl_dashboard_status(self) -> dict:
        async def _impl() -> dict:
            running_ids: list[str] = []
            for campaign_id, info in self.running_tasks.items():
                task = info.get("task")
                if task is None or not task.done():
                    running_ids.append(str(campaign_id))
            telegram_connected = bool(self.tg and self.tg.client.is_connected())
            bot_online = bool(self.bot_mgr and self.bot_mgr.app is not None)
            return {
                "telegram_connected": telegram_connected,
                "bot_online": bot_online,
                "running_ids": running_ids,
            }

        return await self._in_main(_impl())

    async def ctrl_stop_running(self, ad_id: str) -> str:
        async def _impl() -> str:
            info = self.running_tasks.get(ad_id)
            if not info:
                return f"Ad not running\nAd ID: {ad_id}"

            state_path = PROFILES_DIR / f"state_{ad_id}.json"
            stop_state(state_path, ad_id)
            task = info.get("task")
            if task:
                task.cancel()
            self.running_tasks.pop(ad_id, None)
            return f"Stop requested\nAd: {info.get('name') or ad_id}"

        return await self._in_main(_impl())

    async def ctrl_start_ad(self, ad_id: str, dry: bool, force: bool) -> str:
        async def _impl() -> str:
            if self.tg is None:
                return "Not logged in. Please log in first."
            try:
                await self._ensure_account_connected(record=True)
            except Exception as exc:
                return f"Telegram reconnect failed: {type(exc).__name__}: {exc}"

            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"
            if campaign.id in self.running_tasks:
                return f"Ad already running\nAd: {campaign.name}"

            if not getattr(campaign, "enabled", True):
                if not force:
                    return "Ad is disabled. Enable it first or use a force action."
                campaign.enabled = True
                replace_campaign(campaign)

            state_path = PROFILES_DIR / f"state_{campaign.id}.json"
            state = load_state(state_path, campaign.id)
            scheduled_for: Optional[datetime] = None
            if state is not None:
                paused = bool(getattr(state, "paused", False))
                stopped = bool(getattr(state, "stopped", False))
                next_at = _safe_parse_next_at(getattr(state, "next_at", None))

                if (paused or stopped) and not force:
                    parts = []
                    if paused:
                        parts.append("paused")
                    if stopped:
                        parts.append("stopped")
                    return "Ad is not ready to start: " + ", ".join(parts) + ". Use a force action to override."

                if next_at and next_at > datetime.now() and not force:
                    scheduled_for = next_at

                if force:
                    state.paused = False
                    state.paused_at = None
                    state.paused_reason = None
                    state.stopped = False
                    state.stopped_at = None
                    state.next_at = None
                    save_state(state_path, state)

            risk = assess_campaign_risk(campaign)
            if (risk.guardrails or risk.level == "high") and not (force or self.options.allow_risky):
                return "Risk checks require confirmation. Force the start or run the service with --allow-risky."

            ok, msg = await self._start_campaign_background(
                campaign,
                dry=dry,
                allow_risky=bool(force or self.options.allow_risky),
            )
            if not ok:
                return f"Start failed: {msg}"

            mode_txt = "DRY" if dry else "LIVE"
            if scheduled_for:
                return (
                    f"Start command sent\nAd: {campaign.name}\nMode: {mode_txt}\n"
                    f"Next send: {scheduled_for.strftime('%H:%M:%S %d/%m/%Y')}"
                )
            return f"Start command sent\nAd: {campaign.name}\nMode: {mode_txt}"

        return await self._in_main(_impl())

    async def ctrl_resume_schedule(self, ad_id: str) -> str:
        async def _impl() -> str:
            if self.tg is None:
                return "Not logged in. Please log in first."
            try:
                await self._ensure_account_connected(record=True)
            except Exception as exc:
                return f"Telegram reconnect failed: {type(exc).__name__}: {exc}"

            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"
            if campaign.id in self.running_tasks:
                return f"Ad already running\nAd: {campaign.name}"
            if not getattr(campaign, "enabled", True):
                return "Ad is disabled. Enable it first."

            state_path = PROFILES_DIR / f"state_{campaign.id}.json"
            state = load_state(state_path, campaign.id)
            if state is None:
                return "No saved schedule found for this ad. Use Start to begin immediately."

            next_at = _safe_parse_next_at(getattr(state, "next_at", None))
            if not isinstance(next_at, datetime) or next_at <= datetime.now():
                return "No future schedule found for this ad. Use Start to begin immediately."

            state.paused = False
            state.paused_at = None
            state.paused_reason = None
            state.stopped = False
            state.stopped_at = None
            save_state(state_path, state)

            risk = assess_campaign_risk(campaign)
            if (risk.guardrails or risk.level == "high") and not self.options.allow_risky:
                return "Risk checks require confirmation. Re-run the service with --allow-risky or start manually."

            ok, msg = await self._start_campaign_background(
                campaign,
                dry=False,
                allow_risky=self.options.allow_risky,
            )
            if not ok:
                return f"Start failed: {msg}"

            return (
                f"Resume scheduled\nAd: {campaign.name}\n"
                f"Next send: {next_at.strftime('%H:%M:%S %d/%m/%Y')}"
            )

        return await self._in_main(_impl())

    async def ctrl_ad_status(self, ad_id: str) -> str:
        async def _impl() -> str:
            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"

            state_path = PROFILES_DIR / f"state_{campaign.id}.json"
            state = load_state(state_path, campaign.id)
            running = "yes" if campaign.id in self.running_tasks else "no"
            enabled = "yes" if getattr(campaign, "enabled", True) else "no"
            paused = "yes" if state and getattr(state, "paused", False) else "no"
            stopped = "yes" if state and getattr(state, "stopped", False) else "no"
            sent_total = getattr(state, "sent_total", 0) if state else 0
            next_at = _safe_parse_next_at(getattr(state, "next_at", None)) if state else None
            next_txt = next_at.strftime("%H:%M:%S %d/%m/%Y") if isinstance(next_at, datetime) else "-"
            per_hour, per_day = _estimate_send_rates(
                send_gap_min=campaign.send_gap_min_sec,
                send_gap_max=campaign.send_gap_max_sec,
                batch_gap_min=campaign.batch_gap_min_sec,
                batch_gap_max=campaign.batch_gap_max_sec,
                batch_size=max(1, len(campaign.target_refs)),
            )
            return (
                f"Ad status\n"
                f"Ad: {campaign.name}\n"
                f"Enabled: {enabled}\n"
                f"Running: {running}\n"
                f"Paused: {paused}\n"
                f"Stopped: {stopped}\n"
                f"Total sent: {sent_total}\n"
                f"Next send: {next_txt}\n"
                f"Estimated rate: {per_hour:.1f}/hour | {per_day:.1f}/24h"
            )

        return await self._in_main(_impl())

    async def ctrl_enable_ad(self, ad_id: str) -> str:
        async def _impl() -> str:
            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"
            if getattr(campaign, "enabled", True):
                return f"Ad already enabled\nAd: {campaign.name}"
            campaign.enabled = True
            replace_campaign(campaign)
            return f"Ad enabled\nAd: {campaign.name}"

        return await self._in_main(_impl())

    async def ctrl_disable_ad(self, ad_id: str) -> str:
        async def _impl() -> str:
            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"

            campaign.enabled = False
            replace_campaign(campaign)
            info = self.running_tasks.get(campaign.id)
            if info:
                state_path = PROFILES_DIR / f"state_{campaign.id}.json"
                stop_state(state_path, campaign.id)
                task = info.get("task")
                if task:
                    task.cancel()
                self.running_tasks.pop(campaign.id, None)
                return f"Ad disabled and stopped\nAd: {campaign.name}"
            return f"Ad disabled\nAd: {campaign.name}"

        return await self._in_main(_impl())

    async def ctrl_health(self) -> str:
        async def _impl() -> str:
            logged_in = "yes" if self.tg and self.tg.client.is_connected() else "no"
            bot_state = "online" if self.bot_mgr and self.bot_mgr.app is not None else "offline"
            return (
                "System health\n\n"
                f"Logged in: {logged_in}\n"
                f"Running ads: {len(self.running_tasks)}\n"
                f"Bot: {bot_state}"
            )

        return await self._in_main(_impl())

    async def ctrl_list_disabled(self, ad_id: str) -> str:
        async def _impl() -> str:
            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"
            header, lines = _format_disabled_targets(campaign)
            if not lines:
                return f"{header}\n\nNone."
            return header + "\n\n" + "\n".join(lines)

        return await self._in_main(_impl())

    async def ctrl_clear_disabled(self, ad_id: str) -> str:
        async def _impl() -> str:
            campaign = get_campaign(ad_id)
            if not campaign:
                return f"Ad not found\nAd ID: {ad_id}"

            state_path = PROFILES_DIR / f"state_{campaign.id}.json"
            state = load_state(state_path, campaign.id)
            if state is None:
                return "No state found for this ad."

            state.target_disabled = {}
            state.target_fail_counts = {}
            save_state(state_path, state)
            return "Disabled targets cleared."

        return await self._in_main(_impl())


def parse_args(argv: Optional[list[str]] = None) -> ServiceOptions:
    parser = argparse.ArgumentParser(description="Run Telegram Forwarder in non-interactive service mode.")
    parser.add_argument("--dry-run", action="store_true", help="Start campaigns in dry-run mode.")
    parser.add_argument(
        "--allow-risky",
        action="store_true",
        help="Start campaigns even when the built-in risk checks would normally require confirmation.",
    )
    parser.add_argument("--skip-bot", action="store_true", help="Do not start the Telegram control bot.")
    parser.add_argument(
        "--start-all",
        action="store_true",
        help="Start every saved campaign on service boot, even if it is currently marked disabled.",
    )
    args = parser.parse_args(argv)
    return ServiceOptions(
        dry_run=bool(args.dry_run),
        allow_risky=bool(args.allow_risky),
        skip_bot=bool(args.skip_bot),
        start_all=bool(args.start_all),
    )


def main(argv: Optional[list[str]] = None) -> int:
    options = parse_args(argv)
    service = ForwarderService(options)
    return asyncio.run(service.run())
