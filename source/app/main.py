# app/main.py

from __future__ import annotations



import asyncio

import logging

import re

import sqlite3

from datetime import datetime, timedelta

from pathlib import Path

from typing import Any, Dict, Optional, Tuple

from collections import deque

import time



from rich.table import Table



from app.core.telegram_client import TgClient, TgCredentials

from app.core.destinations import sync_destinations, Destination

from app.core.topics import fetch_forum_topics

from app.core.targets import (

    DestinationTarget,

    load_targets,

    save_targets,

    add_targets,

    remove_targets,

    clear_targets_for_group,

)

from app.core.campaigns import (

    Campaign,

    list_campaigns,

    get_campaign,

    save_campaign,

    new_campaign_id,

    replace_campaign,

    delete_campaign,

)

from app.core.runner import run_campaign

from app.core.accounts import (

    list_accounts,

    add_account,

    set_active_account,

    get_active_account,

    delete_account,

    update_account_proxy,

    update_account_advanced,

    get_account,

    list_account_proxies,

    add_account_proxy,

    delete_account_proxy,

    clear_account_proxies,

    update_proxy_rotation_settings,

    pick_proxy_for_account,

)



from app.utils.lock import acquire_lock

from app.ui.menu import print_header, main_menu, render_destinations, console

from app.utils.paths import (

    ensure_folders,

    DESTINATIONS_CACHE,

    PROFILES_DIR,

    DATA_DIR,

    LOCKS_DIR,

    LOGS_DIR,

    EXPORTS_DIR,

    DATABASE_FILE,

)

from app.utils.storage import save_json, load_json

from app.utils.config import AppConfig, load_config, save_config

from app.utils.selection import parse_selection

from app.utils.reset import (

    delete_config,

    delete_destinations_cache,

    delete_targets,

    delete_campaigns,

    delete_sessions,

    nuke_all,

)

from app.utils.state import load_state, save_state, pause_state, resume_state, stop_state

from app.utils.safety import assess_campaign_risk

from app.utils.schedule import is_allowed_now

from app.alerts.telegram_bot import (

    TelegramBotManager,

    BotControl,

    load_bot_config,

    save_bot_config,

    delete_bot_config,

)

from app.utils.settings import AdvancedSettings, load_settings, save_settings, update_last_export

from app.analytics.history_tracker import get_history



logging.getLogger("telethon").setLevel(logging.ERROR)





def _is_back(s: str) -> bool:

    return s.strip().lower() in ("b", "back")





def _read_int(prompt: str) -> Optional[int]:

    while True:

        raw = input(prompt).strip()

        if _is_back(raw):

            return None

        try:

            return int(raw)

        except Exception:

            console.print("[yellow]Please enter a valid number, or 'b' to go back.[/yellow]")





def _read_int_default(prompt: str, default: int) -> Optional[int]:

    while True:

        raw = input(f"{prompt} [{default}]: ").strip()

        if _is_back(raw):

            return None

        if raw == "":

            return default

        try:

            return int(raw)

        except Exception:

            console.print("[yellow]Please enter a valid number, or 'b' to go back.[/yellow]")





def _read_float_default(prompt: str, default: float) -> Optional[float]:

    while True:

        raw = input(f"{prompt} [{default}]: ").strip()

        if _is_back(raw):

            return None

        if raw == "":

            return float(default)

        try:

            return float(raw)

        except Exception:

            console.print("[yellow]Please enter a valid number, or 'b' to go back.[/yellow]")





def _read_optional_int(prompt: str, current: Optional[int]) -> Optional[int]:

    raw = input(f"{prompt} [{current if current is not None else '-'}] (blank keep, 0 clear): ").strip()

    if _is_back(raw):

        return current

    if raw == "":

        return current

    if raw == "0":

        return None

    if raw.isdigit():

        return int(raw)

    console.print("[yellow]Invalid input. Keeping current.[/yellow]")

    return current





def _read_optional_str(prompt: str, current: Optional[str]) -> Optional[str]:

    raw = input(f"{prompt} [{current if current else '-'}] (blank keep, 'none' clear): ").strip()

    if _is_back(raw):

        return current

    if raw == "":

        return current

    if raw.lower() == "none":

        return None

    return raw





def _edit_advanced_settings(settings: AdvancedSettings) -> AdvancedSettings:

    while True:

        console.print("\n[bold]Advanced settings[/bold]")

        console.print("1) Scheduling & pacing")

        console.print("2) Reliability & error control")

        console.print("3) Bot & UI")

        console.print("4) Logging & history")

        console.print("5) Defaults for new ads")

        console.print("6) Back")

        pick = input("Choice: ").strip()



        if pick == "1":

            console.print("\n[bold]Scheduling & pacing[/bold]")

            settings.global_quiet_hours_enabled = (

                input(f"Global quiet hours enabled (y/N) [{ 'y' if settings.global_quiet_hours_enabled else 'n' }]: ")

                .strip()

                .lower()

                == "y"

            )

            settings.global_quiet_start = _read_optional_str("Quiet start HH:MM", settings.global_quiet_start) or settings.global_quiet_start

            settings.global_quiet_end = _read_optional_str("Quiet end HH:MM", settings.global_quiet_end) or settings.global_quiet_end

            settings.global_jitter_min_sec = _read_optional_int("Global jitter min sec", settings.global_jitter_min_sec) or 0

            settings.global_jitter_max_sec = _read_optional_int("Global jitter max sec", settings.global_jitter_max_sec) or 0

            settings.global_min_send_gap_sec = _read_optional_int("Global min time between posts (sec)", settings.global_min_send_gap_sec) or 1

            settings.global_max_msgs_per_hour = _read_optional_int(

                "Global max msgs/hour",

                settings.global_max_msgs_per_hour,

            )

            settings.max_next_at_drift_sec = _read_optional_int("Max next_at drift sec", settings.max_next_at_drift_sec)

            save_settings(settings)

            console.print("[green]Saved.[/green]")



        elif pick == "2":

            console.print("\n[bold]Reliability & error control[/bold]")

            settings.max_error_streak_pause = _read_optional_int("Max error streak auto-pause", settings.max_error_streak_pause)

            settings.kill_switch_error_streak = _read_optional_int(

                "Kill-switch error streak (stop ad)",

                settings.kill_switch_error_streak,

            )

            settings.auto_resume_minutes = _read_optional_int("Auto-resume after minutes", settings.auto_resume_minutes)

            settings.continue_on_target_error = (

                input(f"Continue on target errors (y/N) [{ 'y' if settings.continue_on_target_error else 'n' }]: ")

                .strip()

                .lower()

                == "y"

            )

            settings.retry_transient_count = _read_optional_int("Retry transient errors count", settings.retry_transient_count) or 0

            settings.retry_transient_base_delay_sec = _read_float_default(

                "Retry base delay sec",

                float(settings.retry_transient_base_delay_sec or 2.0),

            ) or settings.retry_transient_base_delay_sec

            settings.cooldown_timeout_sec = _read_optional_int("Timeout cooldown sec", settings.cooldown_timeout_sec) or settings.cooldown_timeout_sec

            settings.cooldown_flood_min_sec = _read_optional_int("Flood wait min sec", settings.cooldown_flood_min_sec) or settings.cooldown_flood_min_sec

            settings.cooldown_flood_max_sec = _read_optional_int("Flood wait max sec", settings.cooldown_flood_max_sec) or settings.cooldown_flood_max_sec

            settings.cooldown_topic_closed_sec = _read_optional_int("Topic closed cooldown sec", settings.cooldown_topic_closed_sec) or settings.cooldown_topic_closed_sec

            settings.cooldown_not_in_forum_sec = _read_optional_int("Not-in-forum cooldown sec", settings.cooldown_not_in_forum_sec) or settings.cooldown_not_in_forum_sec

            settings.cooldown_generic_error_sec = _read_optional_int("Generic error cooldown sec", settings.cooldown_generic_error_sec) or settings.cooldown_generic_error_sec

            settings.latest_source_poll_sec = _read_optional_int("Latest-source poll sec", settings.latest_source_poll_sec) or settings.latest_source_poll_sec

            settings.auto_disable_target_error_count = _read_optional_int(

                "Auto-disable target after errors (count)",

                settings.auto_disable_target_error_count,

            ) or settings.auto_disable_target_error_count

            raw_minutes = input(

                f"Auto-disable target minutes (0 = permanent) [{settings.auto_disable_target_minutes if settings.auto_disable_target_minutes is not None else '-'}]: "

            ).strip()

            if raw_minutes == "":

                pass

            elif raw_minutes == "0":

                settings.auto_disable_target_minutes = None

            elif raw_minutes.isdigit():

                settings.auto_disable_target_minutes = int(raw_minutes)

            settings.auto_disable_on_no_permission = (

                input(

                    f"Auto-disable on no-permission (y/N) [{ 'y' if settings.auto_disable_on_no_permission else 'n' }]: "

                )

                .strip()

                .lower()

                == "y"

            )

            settings.auto_disable_on_dead_group = (

                input(

                    f"Auto-disable on dead/invalid group (y/N) [{ 'y' if settings.auto_disable_on_dead_group else 'n' }]: "

                )

                .strip()

                .lower()

                == "y"

            )

            settings.backoff_step = _read_float_default("Adaptive backoff step", settings.backoff_step or 0.5) or settings.backoff_step

            settings.backoff_max_multiplier = _read_float_default(

                "Adaptive backoff max multiplier",

                settings.backoff_max_multiplier or 4.0,

            ) or settings.backoff_max_multiplier

            save_settings(settings)

            console.print("[green]Saved.[/green]")



        elif pick == "3":

            console.print("\n[bold]Bot & UI[/bold]")

            settings.bot_quiet_hours_enabled = (

                input(f"Bot quiet hours enabled (y/N) [{ 'y' if settings.bot_quiet_hours_enabled else 'n' }]: ")

                .strip()

                .lower()

                == "y"

            )

            settings.bot_quiet_start = _read_optional_str("Bot quiet start HH:MM", settings.bot_quiet_start) or settings.bot_quiet_start

            settings.bot_quiet_end = _read_optional_str("Bot quiet end HH:MM", settings.bot_quiet_end) or settings.bot_quiet_end

            settings.live_update_every_sec = _read_optional_int("Live update every sec", settings.live_update_every_sec) or settings.live_update_every_sec

            save_settings(settings)

            console.print("[green]Saved.[/green]")



        elif pick == "4":

            console.print("\n[bold]Logging & history[/bold]")

            settings.log_retention_days = _read_optional_int("Log retention days", settings.log_retention_days)

            settings.history_retention_days = _read_optional_int("History retention days", settings.history_retention_days)

            settings.auto_export_hours = _read_optional_int("Auto-export hours", settings.auto_export_hours)

            save_settings(settings)

            console.print("[green]Saved.[/green]")



        elif pick == "5":

            console.print("\n[bold]Defaults for new ads[/bold]")

            interval = _read_message_interval(

                settings.default_send_gap_min_sec,

                settings.default_send_gap_max_sec,

            )

            if interval is not None:

                settings.default_send_gap_min_sec, settings.default_send_gap_max_sec = interval

            settings.default_batch_gap_min_sec = 0

            settings.default_batch_gap_max_sec = 0

            settings.default_schedule_days = _read_optional_str("Default schedule days [all/weekday/weekend]", settings.default_schedule_days) or settings.default_schedule_days

            settings.default_sleep_start = _read_optional_str("Default sleep start HH:MM", settings.default_sleep_start) or settings.default_sleep_start

            settings.default_sleep_end = _read_optional_str("Default sleep end HH:MM", settings.default_sleep_end) or settings.default_sleep_end

            save_settings(settings)

            console.print("[green]Saved.[/green]")



        elif pick == "6":

            return settings

        else:

            console.print("[yellow]Invalid choice.[/yellow]")





def _stars_txt(obj) -> str:

    val = getattr(obj, "paid_message_stars", None)

    return f"{val}" if isinstance(val, int) and val > 0 else "-"





def _fmt_next(next_in_sec: Optional[int]) -> str:

    if next_in_sec is None:

        return "-"

    mins = int(next_in_sec) // 60

    secs = int(next_in_sec) % 60

    if mins > 0:

        return f"{mins}m {secs}s"

    return f"{secs}s"





def _safe_parse_next_at(value) -> Optional[datetime]:

    if value is None:

        return None

    if isinstance(value, datetime):

        return value

    if isinstance(value, str):

        s = value.strip()

        if not s:

            return None

        if s.lower() in ("null", "none"):

            return None

        try:

            return datetime.fromisoformat(s)

        except Exception:

            return None

    return None





def _format_schedule(
    days_mode: str,

    windows: Optional[list[dict]],

    sleep_start: str,

    sleep_end: str,

    windows_weekday: Optional[list[dict]] = None,

    windows_weekend: Optional[list[dict]] = None,

) -> str:

    def _win_txt(win: Optional[list[dict]]) -> str:

        if not win:

            return "all day"

        parts = []

        for w in win:

            try:

                parts.append(f"{w.get('start','...')}-{w.get('end','...')}")

            except Exception:

                continue

        return ", ".join(parts) if parts else "custom"



    if windows_weekday is not None or windows_weekend is not None:

        wd_txt = _win_txt(windows_weekday if windows_weekday is not None else windows)

        we_txt = _win_txt(windows_weekend if windows_weekend is not None else windows)

        win_txt = f"weekday={wd_txt}, weekend={we_txt}"

    else:

        win_txt = _win_txt(windows)



    if not sleep_start or not sleep_end:
        sleep_txt = "off"
    else:
        sleep_txt = f"{sleep_start}-{sleep_end}"
    return f"days={days_mode}, windows={win_txt}, sleep={sleep_txt}"


def _friendly_stop_reason(raw: str) -> str:
    key = (raw or "").strip()
    mapping = {
        "stopped": "Stopped by user",
        "no_targets": "No targets configured",
        "no_sources": "No sources configured",
        "no_messages": "No messages configured",
        "all_targets_disabled": "All targets are disabled",
        "drift_exceeded": "Schedule drift exceeded",
    }
    if key in mapping:
        return mapping[key]
    if key.startswith("kill_switch_error_streak"):
        parts = key.split("=", 1)
        if len(parts) == 2 and parts[1].strip():
            return f"Safety stop: error streak reached ({parts[1].strip()})"
        return "Safety stop: error streak reached"
    if not key or key == "-":
        return "Stopped"
    return key.replace("_", " ").strip().capitalize()




def _estimate_send_rates(

    *,

    send_gap_min: int,

    send_gap_max: int,

    batch_gap_min: int,

    batch_gap_max: int,

    batch_size: int,

) -> Tuple[float, float]:

    avg_gap = (send_gap_min + send_gap_max) / 2.0

    if avg_gap <= 0:

        return 0.0, 0.0

    per_hour = 3600.0 / avg_gap

    per_day = per_hour * 24.0

    return per_hour, per_day


def _format_message_duration(seconds: int) -> str:

    total_minutes = max(1, int(round(int(seconds) / 60.0)))

    hours, minutes = divmod(total_minutes, 60)

    if hours and minutes:

        return f"{hours}h {minutes}m"

    if hours:

        return f"{hours}h"

    return f"{minutes}m"


def _format_message_interval(min_seconds: int, max_seconds: int) -> str:

    minimum = _format_message_duration(min_seconds)

    maximum = _format_message_duration(max_seconds)

    return minimum if minimum == maximum else f"{minimum} - {maximum}"


def _parse_message_duration(value: str) -> int:

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mh])\s*", value, flags=re.IGNORECASE)

    if match is None:

        raise ValueError("Use minutes or hours, for example 30m, 2h, or 1.5h.")

    amount = float(match.group(1))

    if amount <= 0:

        raise ValueError("Duration must be greater than zero.")

    multiplier = 60 if match.group(2).casefold() == "m" else 3600

    return max(60, int(round(amount * multiplier)))


def _read_message_interval(default_min: int, default_max: int) -> Optional[Tuple[int, int]]:

    default_label = _format_message_interval(default_min, default_max)

    raw = input(

        f"Message interval [30m, 2h, 12h, or 1h-2h] [{default_label}]: "

    ).strip()

    if not raw:

        return int(default_min), int(default_max)

    if raw.casefold() in {"b", "back", "q", "quit", "cancel"}:

        return None

    parts = [part.strip() for part in raw.split("-", 1)]

    minimum = _parse_message_duration(parts[0])

    maximum = _parse_message_duration(parts[1]) if len(parts) == 2 else minimum

    if maximum < minimum:

        minimum, maximum = maximum, minimum

    return minimum, maximum





def _parse_time_window(raw: str) -> Optional[tuple[str, str]]:

    if raw.lower() in ("none", "disable"):

        return ("00:00", "00:00")

    if "-" not in raw:

        console.print("[yellow]Invalid format. Use HH:MM-HH:MM[/yellow]")

        return None

    start, end = [p.strip() for p in raw.split("-", 1)]

    if len(start) != 5 or len(end) != 5:

        console.print("[yellow]Invalid time format. Use HH:MM[/yellow]")

        return None

    return (start, end)





def _prompt_windows(*, prompt: str = "Time windows") -> Optional[list[dict]]:

    console.print(f"{prompt}:")

    console.print("1) All day")

    console.print("2) Morning (08:00-12:00)")

    console.print("3) Afternoon (12:00-17:00)")

    console.print("4) Evening (17:00-22:00)")

    console.print("5) Morning + Afternoon + Evening")

    console.print("6) Custom windows")

    w_choice = input("Choice [1]: ").strip()

    if w_choice == "2":

        return [{"start": "08:00", "end": "12:00", "label": "morning"}]

    if w_choice == "3":

        return [{"start": "12:00", "end": "17:00", "label": "afternoon"}]

    if w_choice == "4":

        return [{"start": "17:00", "end": "22:00", "label": "evening"}]

    if w_choice == "5":

        return [

            {"start": "08:00", "end": "12:00", "label": "morning"},

            {"start": "12:00", "end": "17:00", "label": "afternoon"},

            {"start": "17:00", "end": "22:00", "label": "evening"},

        ]

    if w_choice == "6":

        console.print("Enter custom windows as HH:MM-HH:MM. Empty line to finish.")

        custom: list[dict] = []

        while True:

            line = input("> ").strip()

            if not line:

                break

            if "-" not in line:

                console.print("[yellow]Invalid format. Use HH:MM-HH:MM[/yellow]")

                continue

            start, end = [p.strip() for p in line.split("-", 1)]

            if len(start) != 5 or len(end) != 5:

                console.print("[yellow]Invalid time format. Use HH:MM[/yellow]")

                continue

            custom.append({"start": start, "end": end, "label": ""})

        return custom if custom else None

    return None





def _prompt_schedule(

    *,

    default_days: str = "all",

    default_windows: Optional[list[dict]] = None,

    default_sleep_start: str = "",

    default_sleep_end: str = "",

) -> tuple[str, Optional[list[dict]], str, str, Optional[list[dict]], Optional[list[dict]]]:

    console.print("[bold]Schedule settings[/bold]")

    console.print("Days mode:")

    console.print("1) All days")

    console.print("2) Weekdays only")

    console.print("3) Weekends only")

    d_choice = input(f"Choice [{default_days}]: ").strip()

    if d_choice == "2":

        days_mode = "weekday"

    elif d_choice == "3":

        days_mode = "weekend"

    elif d_choice == "":

        days_mode = default_days

    else:

        days_mode = "all"



    split = input("Different windows for weekdays/weekends... (y/N): ").strip().lower() == "y"

    windows_weekday: Optional[list[dict]] = None

    windows_weekend: Optional[list[dict]] = None

    if split:

        windows_weekday = _prompt_windows(prompt="Weekday windows")

        windows_weekend = _prompt_windows(prompt="Weekend windows")

        windows = None

    else:

        windows = _prompt_windows() or default_windows



    sleep_prompt = f"Sleep hours HH:MM-HH:MM [{default_sleep_start}-{default_sleep_end}] (or 'none'): "
    sleep_raw = input(sleep_prompt).strip()
    if sleep_raw == "":
        sleep_start, sleep_end = default_sleep_start, default_sleep_end
    elif sleep_raw.lower() in ("none", "off", "no"):
        sleep_start, sleep_end = "", ""
    else:
        parsed = _parse_time_window(sleep_raw)
        if parsed is None:
            sleep_start, sleep_end = default_sleep_start, default_sleep_end
        else:

            sleep_start, sleep_end = parsed



    console.print(

        f"[green]Schedule saved:[/green] "

        f"{_format_schedule(days_mode, windows, sleep_start, sleep_end, windows_weekday, windows_weekend)}"

    )

    return days_mode, windows, sleep_start, sleep_end, windows_weekday, windows_weekend





def _print_accounts() -> None:

    accounts = list_accounts()

    if not accounts:

        console.print("[yellow]No accounts configured yet.[/yellow]")

        return

    console.print("[bold]Accounts[/bold]")

    for a in accounts:

        flag = " (ACTIVE)" if a.is_active else ""

        label = a.label or a.phone

        proxy = f"{a.proxy_type or '-'} {a.proxy_host or ''}:{a.proxy_port or ''}".strip()

        pool_count = len(list_account_proxies(a.id))

        mode = (a.proxy_rotation_mode or "round_robin") if pool_count > 0 else "-"

        rate = getattr(a, "rate_multiplier", None)

        window = ""

        if getattr(a, "send_window_start", None) and getattr(a, "send_window_end", None):

            days = getattr(a, "send_days", "all") or "all"

            window = f" window={days} {a.send_window_start}-{a.send_window_end}"

        console.print(

            f"{a.id}) {label}  phone={a.phone}  proxy={proxy}  pool={pool_count}  mode={mode}  rate={rate or '-'}{window}{flag}"

        )





def _print_accounts_table() -> None:

    accounts = list_accounts()

    if not accounts:

        console.print("[yellow]No accounts configured yet.[/yellow]")

        return

    table = Table(title="Accounts", show_header=True, header_style="bold magenta")

    table.add_column("ID", justify="right", style="dim")

    table.add_column("Label")

    table.add_column("Phone", style="dim")

    table.add_column("Active")

    table.add_column("Proxy")

    table.add_column("Pool")

    table.add_column("ProxyMode")

    table.add_column("Rate")

    table.add_column("Window", style="dim")

    for a in accounts:

        label = a.label or "-"

        active = "yes" if a.is_active else ""

        proxy = f"{a.proxy_type or '-'} {a.proxy_host or ''}:{a.proxy_port or ''}".strip()

        pool_count = str(len(list_account_proxies(a.id)))

        mode = (a.proxy_rotation_mode or "round_robin") if int(pool_count) > 0 else "-"

        rate = str(a.rate_multiplier) if a.rate_multiplier is not None else "-"

        window = "-"

        if a.send_window_start and a.send_window_end:

            days = a.send_days or "all"

            window = f"{days} {a.send_window_start}-{a.send_window_end}"

        table.add_row(str(a.id), label, a.phone, active, proxy, pool_count, mode, rate, window)

    console.print(table)





def _edit_account_basic(a) -> None:

    label = input(f"Label [{a.label or '-'}] (blank keep): ").strip()

    if label:

        a.label = label

    phone = input(f"Phone [{a.phone}] (blank keep): ").strip()

    if phone:

        a.phone = phone

    api_id_raw = input(f"API ID [{a.api_id}] (blank keep): ").strip()

    if api_id_raw:

        if api_id_raw.isdigit():

            a.api_id = int(api_id_raw)

        else:

            console.print("[yellow]Invalid API ID. Keeping current.[/yellow]")

    api_hash = input("API HASH (blank keep): ").strip()

    if api_hash:

        a.api_hash = api_hash

    # update by delete + add pattern not supported; re-use update_account_advanced for new fields only.

    # Basic fields are stored in accounts DB; update via SQL directly.

    conn = sqlite3.connect(str(DATABASE_FILE))

    conn.row_factory = sqlite3.Row

    try:

        conn.execute(

            """

            UPDATE accounts

            SET label = ?, api_id = ?, api_hash = ?, phone = ?

            WHERE id = ?

            """,

            (a.label, a.api_id, a.api_hash, a.phone, a.id),

        )

        conn.commit()

    finally:

        conn.close()





def _prompt_proxy_fields() -> tuple[Optional[str], Optional[str], Optional[int], Optional[str], Optional[str]]:

    console.print("Proxy type:")

    console.print("1) SOCKS5 (recommended)")

    console.print("2) SOCKS4")

    console.print("3) HTTP")

    raw = input("Choice [1] or blank for none: ").strip().lower()

    if not raw:

        return None, None, None, None, None

    if raw in ("1", "socks5", "socks"):

        ptype = "socks5"

    elif raw in ("2", "socks4"):

        ptype = "socks4"

    elif raw in ("3", "http", "https"):

        ptype = "http"

    else:

        console.print("[yellow]Invalid proxy type. Proxy cleared.[/yellow]")

        return None, None, None, None, None

    host = input("Proxy host: ").strip()

    port_raw = input("Proxy port: ").strip()

    if not host or not port_raw.isdigit():

        console.print("[yellow]Invalid proxy host/port. Proxy cleared.[/yellow]")

        return None, None, None, None, None

    user = input("Proxy username (optional): ").strip()

    pw = input("Proxy password (optional): ").strip()

    return ptype, host, int(port_raw), user or None, pw or None





def _print_proxy_pool(account_id: int) -> None:

    proxies = list_account_proxies(account_id)

    if not proxies:

        console.print("[yellow]No proxy pool entries for this account.[/yellow]")

        return

    console.print("[bold]Proxy pool[/bold]")

    for p in proxies:

        label = f" {p.label}" if p.label else ""

        host = f"{p.proxy_host}:{p.proxy_port}" if p.proxy_host and p.proxy_port else "-"

        ptype = p.proxy_type or "-"

        console.print(f"{p.id}){label} {ptype} {host}")





def _accounts_menu() -> str:

    console.print("\n[bold]Manage accounts[/bold]")

    console.print("1) Add account")

    console.print("2) List accounts (table)")

    console.print("3) Set active account")

    console.print("4) Edit account (label/phone/api)")

    console.print("5) Update proxy (single)")

    console.print("6) Manage proxy pool")

    console.print("7) Proxy rotation settings")

    console.print("8) Update rate/window")

    console.print("9) Delete account")

    console.print("10) Back")

    return input("Choice: ").strip()





def _load_stars_map() -> Dict[int, Optional[int]]:

    cached = load_json(DESTINATIONS_CACHE, default=[])

    out: Dict[int, Optional[int]] = {}

    for d in cached:

        try:

            gid = int(d.get("id"))

        except Exception:

            continue

        v = d.get("paid_message_stars", None)

        if v is None:

            out[gid] = None

        else:

            try:

                out[gid] = int(v)

            except Exception:

                out[gid] = None

    return out





def _is_paid_target(t: DestinationTarget) -> bool:

    if getattr(t, "is_paid", False):

        return True

    v = getattr(t, "paid_message_stars", None)

    return isinstance(v, int) and v > 0





def _target_paid_tag(t: DestinationTarget) -> str:

    if _is_paid_target(t):

        cost = getattr(t, "paid_message_stars", None)

        if isinstance(cost, int) and cost > 0:

            return f" [stars {cost}]"

        return " [stars]"

    return ""





async def _retry_if_db_locked(fn, *, tries: int = 6, base_delay: float = 0.4):

    """

    Telethon writes entities into the session SQLite during many API calls.

    If another process is using the same session file, we can get 'database is locked'.

    We retry a few times with backoff, then fail with a clean message.

    """

    last_err: Exception | None = None

    for attempt in range(tries):

        try:

            return await fn()

        except sqlite3.OperationalError as e:

            last_err = e

            if "database is locked" not in str(e).lower():

                raise

            await asyncio.sleep(base_delay * (attempt + 1))

    raise RuntimeError(

        "Telegram session database is locked. Close any other running Telegram Forwarder instances and try again."
    ) from last_err







def _maybe_force_start_now(*, state_path: Path, ad_id: str, allow_prompt: bool = True) -> None:

    state = load_state(state_path, ad_id)

    if not state:

        return



    raw_next = getattr(state, "next_at", None)

    parsed = _safe_parse_next_at(raw_next)



    if raw_next is not None and parsed is None:

        console.print("[yellow]Fixed state: next_at was invalid. Starting immediately.[/yellow]")

        state.next_at = None

        save_state(state_path, state)

        return



    if parsed is None:

        return



    if not isinstance(getattr(state, "next_at", None), datetime):

        state.next_at = parsed

        save_state(state_path, state)



    now = datetime.now()

    if parsed > now:

        if not allow_prompt:

            return

        ans = input(
            f"Existing schedule found, next run at {parsed.strftime('%H:%M:%S %d/%m/%Y')}. "
            "Resume schedule (keep cooldown) [Enter], start now [S]: "
        ).strip().lower()

        if ans in ("s", "start", "now", "y"):

            state.next_at = None

            save_state(state_path, state)

            console.print("[green]Starting immediately.[/green]")





def _maybe_resume_if_paused(*, state_path: Path, ad_id: str, allow_prompt: bool = True) -> None:

    st = load_state(state_path, ad_id)

    if st is None:

        return

    if not getattr(st, "paused", False):

        return

    if not allow_prompt:

        return

    ans = input("This Ad is currently PAUSED from a previous run. Resume now... (y/N): ").strip().lower()

    if ans == "y":

        ok = resume_state(state_path, ad_id)

        console.print("[green]Resumed.[/green]" if ok else "[yellow]Could not resume.[/yellow]")





def _maybe_clear_stopped(*, state_path: Path, ad_id: str, allow_prompt: bool = True) -> None:

    st = load_state(state_path, ad_id)

    if st is None:

        return

    if not getattr(st, "stopped", False):

        return

    if not allow_prompt:

        return

    parsed = _safe_parse_next_at(getattr(st, "next_at", None))
    sched_txt = parsed.strftime("%H:%M:%S %d/%m/%Y") if isinstance(parsed, datetime) else "not set"
    ans = input(
        f"This ad is marked STOPPED. Resume schedule ({sched_txt}) [Enter], start now [S], cancel [C]: "
    ).strip().lower()

    if ans in ("c", "cancel", "n", "no"):
        return

    st.stopped = False

    st.stopped_at = None

    st.paused = False

    st.paused_at = None

    if ans in ("s", "start", "now", "y"):
        st.next_at = None

    save_state(state_path, st)

    console.print("[green]Stop flag cleared.[/green]")



def _print_targets(targets: list[DestinationTarget]) -> None:

    console.print("[bold]Saved destination targets[/bold]")

    for i, t in enumerate(targets, start=1):

        tag = _target_paid_tag(t)

        extra = ""

        if isinstance(getattr(t, "extra_delay_sec", None), int) and int(t.extra_delay_sec) > 0:

            extra = f" [delay {int(t.extra_delay_sec)}s]"

        if t.topic_id is None:

            console.print(f"{i}) {t.group_title} (no topic){tag}{extra}")

        else:

            console.print(f"{i}) {t.group_title} -> {t.topic_title}{tag}{extra}")





def _manage_targets_menu() -> str:

    console.print("\n[bold]Manage destination targets[/bold]")

    console.print("1) Delete selected targets (by number)")

    console.print("2) Delete ALL targets for a group (by group number from destinations cache)")

    console.print("3) Set per-target extra delay (seconds)")

    console.print("4) Back")

    return input("Choice: ").strip()





def _delete_destinations_menu() -> str:

    console.print("\n[bold]Delete destinations[/bold]")

    console.print("1) Delete saved destination targets")

    console.print("2) Delete destinations cache (synced list)")

    console.print("3) Delete config (API credentials)")

    console.print("4) Back")

    return input("Choice: ").strip()





def _pick_ad(ads: list[Campaign], prompt: str = "Ad number: ") -> Optional[Campaign]:

    console.print("[bold]Pick an Ad[/bold]")

    for i, c in enumerate(ads, start=1):

        console.print(f"{i}) {c.name} (id={c.id})")

    pick = input(prompt).strip()

    if not pick.isdigit():

        console.print("[yellow]Invalid input.[/yellow]")

        return None

    idx = int(pick)

    if idx < 1 or idx > len(ads):

        console.print("[yellow]Out of range.[/yellow]")

        return None

    return ads[idx - 1]





def _edit_ad_flow(c: Campaign) -> Optional[Campaign]:

    while True:

        console.print()

        console.print(f"[bold]Edit Ad:[/bold] {c.name} (id={c.id})")

        console.print(f"Enabled: {'yes' if getattr(c, 'enabled', True) else 'no'}")

        console.print(f"Latest source mode: {'yes' if getattr(c, 'use_latest_source', False) else 'no'}")

        console.print(f"Sources: {len(getattr(c, 'latest_sources', []) or [])}")

        console.print(f"Messages: {len(c.message_links)} | Targets: {len(c.target_refs)}")

        console.print("1) Rename ad")

        console.print("2) Replace message links")

        console.print("3) Latest-source settings")

        console.print("4) View current targets")

        console.print("5) Manage target selection")

        console.print("6) Edit schedule")

        console.print("7) Edit message interval")

        console.print("8) Toggle enabled/disabled")

        console.print("9) Advanced limits & alerts")

        console.print("10) Delete ad")

        console.print("11) Disabled targets")

        console.print("12) Back")



        sub = input("Choice: ").strip()

        if sub == "1":

            name = input("New ad name (or 'b' to go back): ").strip()

            if _is_back(name):

                continue

            if not name:

                console.print("[yellow]Name cannot be empty.[/yellow]")

                continue

            c.name = name

            replace_campaign(c)

            console.print("[green]Ad renamed.[/green]")



        elif sub == "2":

            console.print("[bold]Current message links:[/bold]")

            for i, link in enumerate(c.message_links, start=1):

                console.print(f"{i}) {link}")

            console.print("Paste new message links one per line. Empty line to finish. Type 'b' to cancel.")

            links: list[str] = []

            back_out = False

            while True:

                line = input().strip()

                if not line:

                    break

                if _is_back(line) and not links:

                    back_out = True

                    break

                if _is_back(line):

                    console.print("[yellow]You already started adding links. Finish with an empty line.[/yellow]")

                    continue

                links.append(line)

            if back_out:

                continue

            if not links:

                console.print("[yellow]No message links added.[/yellow]")

                continue

            c.message_links = links

            replace_campaign(c)

            console.print("[green]Updated message links.[/green]")



        elif sub == "3":

            while True:

                use_latest = bool(getattr(c, "use_latest_source", False))

                sources = list(getattr(c, "latest_sources", []) or [])

                console.print("\n[bold]Latest-source settings[/bold]")

                console.print(f"Enabled: {'yes' if use_latest else 'no'}")

                console.print(f"Sources: {len(sources)}")

                console.print("1) Toggle latest-source mode")

                console.print("2) Replace latest sources")

                console.print("3) Back")

                sub2 = input("Choice: ").strip()

                if sub2 == "1":

                    c.use_latest_source = not use_latest

                    if c.use_latest_source and not sources:

                        console.print("[yellow]No sources set. Latest-source remains OFF until sources are added.[/yellow]")

                        c.use_latest_source = False

                    replace_campaign(c)

                    console.print(

                        f"[green]Latest-source mode: {'ON' if c.use_latest_source else 'OFF'}[/green]"

                    )

                elif sub2 == "2":

                    console.print(

                        "Paste source channels/groups one per line (t.me/..., @username, or -100... ids). "

                        "Empty line to finish. Type 'b' to cancel."

                    )

                    new_sources: list[str] = []

                    back_out = False

                    while True:

                        line = input().strip()

                        if not line:

                            break

                        if _is_back(line) and not new_sources:

                            back_out = True

                            break

                        if _is_back(line):

                            console.print("[yellow]You already started adding sources. Finish with an empty line.[/yellow]")

                            continue

                        new_sources.append(line)

                    if back_out:

                        continue

                    if not new_sources:

                        console.print("[yellow]No sources added.[/yellow]")

                        continue

                    c.latest_sources = new_sources

                    replace_campaign(c)

                    console.print("[green]Updated latest sources.[/green]")

                else:

                    break



        elif sub == "4":

            if not c.target_refs:

                console.print("[yellow]No targets for this ad.[/yellow]")

            else:

                console.print("[bold]Current targets[/bold]")

                for i, t in enumerate(c.target_refs, start=1):

                    console.print(f"{i}) {_label_target(t)}")

            input("Press Enter to continue...")



        elif sub == "5":

            while True:

                console.print("\n[bold]Target selection[/bold]")

                console.print("1) Replace targets")

                console.print("2) Add targets")

                console.print("3) Remove targets")

                console.print("4) Back")

                sub_t = input("Choice: ").strip()



                if sub_t in ("1", "2"):

                    targets = load_targets()

                    if not targets:

                        console.print("[yellow]No destination targets saved yet.[/yellow]")

                        continue

                    _print_targets(targets)

                    raw_sel = input("Select targets (example 1,3,5-9 or all). 'b' to go back: ").strip()

                    if _is_back(raw_sel):

                        continue

                    idxs = parse_selection(raw_sel, max_index=len(targets))

                    if not idxs:

                        console.print("[yellow]No targets selected.[/yellow]")

                        continue

                    chosen = [targets[i - 1] for i in idxs]

                    if sub_t == "1":

                        c.target_refs = [t.__dict__ for t in chosen]

                    else:

                        existing = c.target_refs or []

                        seen = {(int(t.get("group_id")), int(t.get("topic_id") or 0)) for t in existing}

                        for t in chosen:

                            key = (int(t.group_id), int(t.topic_id or 0))

                            if key in seen:

                                continue

                            existing.append(t.__dict__)

                            seen.add(key)

                        c.target_refs = existing

                    replace_campaign(c)

                    console.print("[green]Target selection updated.[/green]")

                    continue



                if sub_t == "3":

                    if not c.target_refs:

                        console.print("[yellow]No targets in this ad.[/yellow]")

                        continue

                    console.print("[bold]Current targets[/bold]")

                    for i, t in enumerate(c.target_refs, start=1):

                        console.print(f"{i}) {_label_target(t)}")

                    raw_sel = input("Remove which targets... (example 1,3,5-9 or all). 'b' to go back: ").strip()

                    if _is_back(raw_sel):

                        continue

                    idxs = parse_selection(raw_sel, max_index=len(c.target_refs))

                    if not idxs:

                        console.print("[yellow]No targets selected.[/yellow]")

                        continue

                    kill = set(i - 1 for i in idxs)

                    c.target_refs = [t for i, t in enumerate(c.target_refs) if i not in kill]

                    replace_campaign(c)

                    console.print("[green]Removed selected targets.[/green]")

                    continue



                if sub_t == "4":

                    break



                console.print("[yellow]Invalid choice.[/yellow]")



        elif sub == "6":

            days_mode, windows, sleep_start, sleep_end, windows_weekday, windows_weekend = _prompt_schedule(

                default_days=getattr(c, "schedule_days", "all"),

                default_windows=getattr(c, "schedule_windows", None),

                default_sleep_start=getattr(c, "sleep_start", ""),

                default_sleep_end=getattr(c, "sleep_end", ""),

            )

            c.schedule_days = days_mode

            c.schedule_windows = windows

            c.schedule_windows_weekday = windows_weekday

            c.schedule_windows_weekend = windows_weekend

            c.sleep_start = sleep_start

            c.sleep_end = sleep_end

            replace_campaign(c)

            console.print("[green]Updated schedule.[/green]")



        elif sub == "7":

            interval = _read_message_interval(c.send_gap_min_sec, c.send_gap_max_sec)

            if interval is None:

                continue

            c.send_gap_min_sec, c.send_gap_max_sec = interval

            c.batch_gap_min_sec = 0

            c.batch_gap_max_sec = 0

            replace_campaign(c)

            console.print("[green]Updated message interval.[/green]")



        elif sub == "8":

            c.enabled = not bool(getattr(c, "enabled", True))

            replace_campaign(c)

            console.print(f"[green]Enabled: {'yes' if c.enabled else 'no'}[/green]")



        elif sub == "9":

            console.print("\n[bold]Advanced limits & alerts[/bold]")

            console.print(f"Daily cap: {getattr(c, 'daily_cap', None) or '-'}")

            console.print(f"Max msgs/hour: {getattr(c, 'max_msgs_per_hour', None) or '-'}")

            console.print(f"Per-target daily cap: {getattr(c, 'per_target_daily_cap', None) or '-'}")

            console.print(f"Per-target cooldown (sec): {getattr(c, 'per_target_cooldown_sec', None) or '-'}")

            bam = getattr(c, "bot_alert_mode", None) or "default"

            ben = getattr(c, "bot_alert_every_n", None) or "-"

            console.print(f"Bot alert mode: {bam} (every_n={ben})")

            console.print(f"Adaptive backoff: {'on' if getattr(c, 'adaptive_backoff_enabled', True) else 'off'}")

            console.print(f"Warm-up: {'on' if getattr(c, 'warmup_enabled', False) else 'off'}")

            console.print(f"Warm-up minutes: {getattr(c, 'warmup_minutes', None) or '-'}")

            console.print(f"Warm-up start mult: {getattr(c, 'warmup_start_multiplier', None) or '-'}")

            console.print(f"Warm-up end mult: {getattr(c, 'warmup_end_multiplier', None) or '-'}")



            raw = input("Daily cap (blank keep, 0 clear): ").strip()

            if raw:

                if raw == "0":

                    c.daily_cap = None

                elif raw.isdigit():

                    c.daily_cap = int(raw)



            raw = input("Max msgs/hour (blank keep, 0 clear): ").strip()

            if raw:

                if raw == "0":

                    c.max_msgs_per_hour = None

                elif raw.isdigit():

                    c.max_msgs_per_hour = int(raw)



            raw = input("Per-target daily cap (blank keep, 0 clear): ").strip()

            if raw:

                if raw == "0":

                    c.per_target_daily_cap = None

                elif raw.isdigit():

                    c.per_target_daily_cap = int(raw)



            raw = input("Per-target cooldown sec (blank keep, 0 clear): ").strip()

            if raw:

                if raw == "0":

                    c.per_target_cooldown_sec = None

                elif raw.isdigit():

                    c.per_target_cooldown_sec = int(raw)



            mode = input("Bot alert mode [default/every/summary/errors] (blank keep): ").strip().lower()

            if mode:

                if mode in ("default", "none"):

                    c.bot_alert_mode = None

                elif mode in ("every", "summary", "errors"):

                    c.bot_alert_mode = mode

            if (c.bot_alert_mode or "") == "summary":

                raw = input("Summary every N sends (blank keep): ").strip()

                if raw.isdigit() and int(raw) > 0:

                    c.bot_alert_every_n = int(raw)

            elif mode in ("default", "none"):

                c.bot_alert_every_n = None



            raw = input("Adaptive backoff (y/N, blank keep): ").strip().lower()

            if raw == "y":

                c.adaptive_backoff_enabled = True

            elif raw == "n":

                c.adaptive_backoff_enabled = False



            raw = input("Warm-up enabled (y/N, blank keep): ").strip().lower()

            if raw == "y":

                c.warmup_enabled = True

            elif raw == "n":

                c.warmup_enabled = False



            raw = input("Warm-up minutes (blank keep, 0 clear): ").strip()

            if raw:

                if raw == "0":

                    c.warmup_minutes = None

                elif raw.isdigit():

                    c.warmup_minutes = int(raw)



            raw = input("Warm-up start multiplier (blank keep): ").strip()

            if raw:

                try:

                    c.warmup_start_multiplier = float(raw)

                except Exception:

                    pass



            raw = input("Warm-up end multiplier (blank keep): ").strip()

            if raw:

                try:

                    c.warmup_end_multiplier = float(raw)

                except Exception:

                    pass



            replace_campaign(c)

            console.print("[green]Advanced settings saved.[/green]")



        elif sub == "10":

            ans = input("Delete this ad permanently... (y/N): ").strip().lower()

            if ans == "y":

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                try:

                    stop_state(state_path, c.id)

                except Exception:

                    pass

                deleted = delete_campaign(c.id)

                try:

                    state_path.unlink(missing_ok=True)

                except Exception:

                    pass

                console.print(

                    "[green]Ad deleted. Saved definition removed, state file cleared, history kept.[/green]"

                    if deleted

                    else "[yellow]Ad not found.[/yellow]"

                )

                return None

        elif sub == "11":

            header, lines = _format_disabled_targets(c)

            console.print(f"\n[bold]{header}[/bold]")

            if not lines:

                console.print("[dim]None[/dim]")

            else:

                for line in lines:

                    console.print(line)

                ans = input("Clear disabled targets... (y/N): ").strip().lower()

                if ans == "y":

                    state_path = PROFILES_DIR / f"state_{c.id}.json"

                    st = load_state(state_path, c.id)

                    if st is not None:

                        st.target_disabled = {}

                        st.target_fail_counts = {}

                        save_state(state_path, st)

                        console.print("[green]Disabled targets cleared.[/green]")

            continue

        elif sub == "12":

            return c

        else:

            console.print("[yellow]Invalid choice.[/yellow]")





async def _run_campaign_flow(

    *,

    tg: TgClient,

    dry: bool,

    bot_mgr: TelegramBotManager | None,

    suppress_updates_fn: Optional[callable] = None,

    campaign: Optional[Campaign] = None,

    event_sink: Optional[callable] = None,

    interactive: bool = True,

    allow_risky: bool = False,

    show_risk: bool = True,

    settings: Optional[AdvancedSettings] = None,

) -> None:

    if settings is None:

        settings = load_settings()

    if campaign is None:

        ads = list_campaigns()

        if not ads:

            console.print("[yellow]No ads yet. Use option 9.[/yellow]")

            return

        c = _pick_ad(ads)

        if c is None:

            return

    else:

        c = campaign



    if not getattr(c, "enabled", True):

        if not interactive:

            console.print("[yellow]Ad is disabled. Enable it before starting.[/yellow]")

            return

        ans = input("This ad is disabled. Enable and run... (y/N): ").strip().lower()

        if ans != "y":

            return

        c.enabled = True

        replace_campaign(c)



    risk = assess_campaign_risk(c)

    if show_risk:

        console.print(

            f"[bold]Risk level:[/bold] {risk.level.upper()} (score {risk.score})"

        )

        for r in risk.reasons:

            console.print(f"[yellow]- {r}[/yellow]")

    if risk.guardrails:

        console.print("[red]Guardrails triggered:[/red]")

        for g in risk.guardrails:

            console.print(f"[red]- {g}[/red]")

        if not interactive and not allow_risky:

            console.print("[yellow]Risk guardrails active. Start canceled.[/yellow]")

            return

        ans = input("Proceed anyway... (y/N): ").strip().lower()

        if ans != "y":

            return

    elif risk.level == "high":

        if not interactive and not allow_risky:

            console.print("[yellow]High risk detected. Start canceled.[/yellow]")

            return

        ans = input("High risk detected. Proceed anyway... (y/N): ").strip().lower()

        if ans != "y":

            return



    console.print(f"[bold]Running Ad:[/bold] {c.name}")

    console.print(f"Mode: {'DRY RUN (no sending)' if dry else 'LIVE sending'}")

    console.print("Press Ctrl+C to stop and return to menu.")

    _save_last_run(ad_id=c.id, ad_name=c.name, mode=("DRY" if dry else "LIVE"))



    try:

        state_path = PROFILES_DIR / f"state_{c.id}.json"



        _maybe_resume_if_paused(state_path=state_path, ad_id=c.id, allow_prompt=interactive)

        _maybe_clear_stopped(state_path=state_path, ad_id=c.id, allow_prompt=interactive)

        _maybe_force_start_now(state_path=state_path, ad_id=c.id, allow_prompt=interactive)



        bot_cfg = load_bot_config()

        if not bot_cfg or not bot_cfg.get("enabled", False):

            bot_cfg = None

        _, on_event = _status_callback_factory(

            c,

            dry=dry,

            bot_mgr=bot_mgr,

            bot_cfg=bot_cfg,

            settings=settings,

            suppress_updates_fn=suppress_updates_fn,

            event_sink=event_sink,

        )



        # account pacing/window defaults

        account_rate = 1.0

        account_schedule = None

        try:

            active = get_active_account()

            if active:

                account_rate = float(getattr(active, "rate_multiplier", None) or settings.account_rate_multiplier_default)

                if getattr(active, "send_window_start", None) and getattr(active, "send_window_end", None):

                    account_schedule = {

                        "days_mode": getattr(active, "send_days", "all") or "all",

                        "windows": None,

                        "sleep_start": getattr(active, "send_window_start"),

                        "sleep_end": getattr(active, "send_window_end"),

                    }

        except Exception:

            pass



        await run_campaign(

            tg_client=tg.client,

            campaign=c,

            state_path=state_path,

            dry_run=dry,

            seed=None,

            on_event=on_event,

            settings=settings,

            account_rate_multiplier=account_rate,

            account_schedule=account_schedule,

            reconnect_minutes=getattr(settings, "force_reconnect_minutes", None),

        )

    except KeyboardInterrupt:

        # Clear any pause/stop flags and schedule so the ad can restart immediately.

        try:

            st = load_state(state_path, c.id)

            if st is not None:

                st.paused = False

                st.paused_at = None

                st.stopped = False

                st.stopped_at = None

                st.next_at = None

                save_state(state_path, st)

        except Exception:

            pass

        console.print("[yellow]Stopped. Returning to menu.[/yellow]")

    except Exception as e:

        console.print(f"[red]Runner error:[/red] {e}")





def _show_ad_status() -> None:

    ads = list_campaigns()

    if not ads:

        console.print("[yellow]No Ads yet.[/yellow]")

        return



    table = Table(title="Ad status")

    table.add_column("#", justify="right")

    table.add_column("Name")

    table.add_column("ID")

    table.add_column("Enabled")

    table.add_column("State")

    table.add_column("Sent")

    table.add_column("Next at")

    table.add_column("Paused at")



    for i, c in enumerate(ads, start=1):

        state_path = PROFILES_DIR / f"state_{c.id}.json"

        st = load_state(state_path, c.id)



        enabled_txt = "yes" if getattr(c, "enabled", True) else "no"

        if st is None:

            state_txt = "idle"

            sent_txt = "0"

            next_txt = "-"

            paused_txt = "-"

        else:

            paused = bool(getattr(st, "paused", False))

            stopped = bool(getattr(st, "stopped", False))

            if stopped:

                state_txt = "stopped"

            elif paused:

                state_txt = "paused"

            else:

                state_txt = "running"

            sent_txt = str(getattr(st, "sent_total", 0) or 0)

            na = _safe_parse_next_at(getattr(st, "next_at", None))

            next_txt = na.strftime("%H:%M:%S %d/%m/%Y") if isinstance(na, datetime) else "-"
            pa = _safe_parse_next_at(getattr(st, "paused_at", None))

            paused_txt = pa.strftime("%H:%M:%S %d/%m/%Y") if isinstance(pa, datetime) else "-"


        table.add_row(str(i), c.name, str(c.id), enabled_txt, state_txt, sent_txt, next_txt, paused_txt)



    console.print(table)





def _ensure_exports_dir() -> Path:

    exports_dir = DATA_DIR / "exports"

    exports_dir.mkdir(parents=True, exist_ok=True)

    return exports_dir





def _export_all() -> None:

    exports_dir = _ensure_exports_dir()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")



    targets = load_targets()

    targets_path = exports_dir / f"targets_export_{ts}.json"

    save_json(targets_path, [t.__dict__ for t in targets])



    ads = list_campaigns()

    ads_path = exports_dir / f"ads_export_{ts}.json"

    save_json(ads_path, [c.__dict__ for c in ads])



    console.print("[green]Export completed.[/green]")

    console.print(f"Targets: {targets_path}")

    console.print(f"Ads: {ads_path}")





def _import_targets_from(path: Path) -> None:

    raw = load_json(path, default=None)

    if not isinstance(raw, list):

        console.print("[red]Invalid targets file, expected a list.[/red]")

        return



    incoming: list[DestinationTarget] = []

    for t in raw:

        try:

            topic_id_raw = t.get("topic_id", None)

            top_msg_raw = t.get("topic_top_message", None)

            paid_raw = t.get("paid_message_stars", None)

            is_paid_raw = t.get("is_paid", None)



            incoming.append(

                DestinationTarget(

                    group_id=int(t["group_id"]),

                    group_title=str(t.get("group_title", "")),

                    topic_id=(int(topic_id_raw) if topic_id_raw is not None else None),

                    topic_title=(str(t["topic_title"]) if t.get("topic_title", None) is not None else None),

                    topic_top_message=(int(top_msg_raw) if top_msg_raw is not None else None),

                    paid_message_stars=(int(paid_raw) if paid_raw is not None else None),

                    is_paid=(bool(is_paid_raw) if is_paid_raw is not None else False),

                )

            )

        except Exception:

            continue



    existing = load_targets()

    merged = add_targets(existing, incoming)

    save_targets(merged)



    console.print(f"[green]Imported targets.[/green] Added {len(merged) - len(existing)} new targets.")





def _import_ads_from(path: Path) -> None:

    raw = load_json(path, default=None)

    if not isinstance(raw, list):

        console.print("[red]Invalid Ads file, expected a list.[/red]")

        return



    count = 0

    for d in raw:

        try:

            c = Campaign.from_dict(d)

            save_campaign(c)

            count += 1

        except Exception:

            continue



    console.print(f"[green]Imported Ads.[/green] Saved {count} Ads.")





def _import_all() -> None:

    console.print("Paste targets export path (or blank to skip):")

    tpath = input("> ").strip()

    if tpath:

        _import_targets_from(Path(tpath))



    console.print("Paste Ads export path (or blank to skip):")

    cpath = input("> ").strip()

    if cpath:

        _import_ads_from(Path(cpath))





def _split_target_label(label: Optional[str]) -> Tuple[str, str]:

    if not label:

        return "-", "-"

    if "->" in label:

        left, right = label.split("->", 1)

        group = left.strip()

        topic = right.strip()

        return group, topic

    if "(no topic)" in label:

        group = label.replace("(no topic)", "").strip()

        return group, "-"

    return label.strip(), "-"





def _label_target(t: dict) -> str:

    gt = str(t.get("group_title", "Unknown"))

    tt = t.get("topic_title", None)

    if tt:

        return f"{gt} -> {tt}"

    return f"{gt} (no topic)"





def _target_key_from_ref(t: dict) -> str:

    gid = int(t.get("group_id"))

    tid = t.get("topic_id", None)

    tid = int(tid) if tid is not None else 0

    return f"{gid}:{tid}"





def _format_disabled_targets(c: Campaign) -> Tuple[str, list[str]]:

    state_path = PROFILES_DIR / f"state_{c.id}.json"

    st = load_state(state_path, c.id)

    disabled = getattr(st, "target_disabled", None) if st else None

    if not disabled:

        return "No disabled targets.", []

    label_map: Dict[str, str] = {}

    for t in c.target_refs:

        key = _target_key_from_ref(t)

        label = _label_target(t)

        label_map[key] = label

    lines: list[str] = []

    for key, info in disabled.items():

        reason = str(info.get("reason", "unknown"))

        until = info.get("until", None)

        if until:

            try:

                dt = datetime.fromisoformat(until)

                until_txt = dt.strftime("%H:%M:%S %d/%m/%Y")
            except Exception:

                until_txt = str(until)

        else:

            until_txt = "permanent"

        label = label_map.get(key, key)

        lines.append(f"- {label} | reason={reason} | until={until_txt}")

    return "Disabled targets:", lines





def _print_ad_header_block(ad: Campaign, *, dry: bool, evt: Dict[str, Any]) -> None:

    mode_txt = "DRY RUN (no sending)" if dry else "LIVE sending"



    targets_total = evt.get("targets_total", "...")

    send_gap_min = evt.get("send_gap_min_sec", getattr(ad, "send_gap_min_sec", "..."))

    send_gap_max = evt.get("send_gap_max_sec", getattr(ad, "send_gap_max_sec", "..."))



    sched_days = evt.get("schedule_days", getattr(ad, "schedule_days", "all"))

    sched_windows = evt.get("schedule_windows", getattr(ad, "schedule_windows", None))

    sleep_start = evt.get("sleep_start", getattr(ad, "sleep_start", ""))

    sleep_end = evt.get("sleep_end", getattr(ad, "sleep_end", ""))



    console.print()

    console.print(f"[bold]Ad started:[/bold] {ad.name}  (id={ad.id})")

    console.print(f"Mode: {mode_txt}")

    console.print(f"Targets: {targets_total}")

    if all(isinstance(v, int) for v in (send_gap_min, send_gap_max)):

        console.print(f"Message interval: {_format_message_interval(send_gap_min, send_gap_max)}")

        per_hour, per_day = _estimate_send_rates(

            send_gap_min=send_gap_min,

            send_gap_max=send_gap_max,

            batch_gap_min=0,

            batch_gap_max=0,

            batch_size=1,

        )

        console.print(f"Estimated sends: {per_hour:.1f}/hour | {per_day:.1f}/24h")

        # Progress + ETA for today (if daily cap is set)

        daily_cap = getattr(ad, "daily_cap", None)

        if isinstance(daily_cap, int) and daily_cap > 0:

            state_path = PROFILES_DIR / f"state_{ad.id}.json"

            st = load_state(state_path, ad.id)

            sent_today = int(getattr(st, "day_sent_count", 0) or 0) if st else 0

            remaining = max(0, int(daily_cap) - sent_today)

            console.print(f"Progress today: {sent_today}/{daily_cap} (remaining {remaining})")

            if per_hour > 0 and remaining > 0:

                eta_hours = remaining / float(per_hour)

                eta_minutes = int(round(eta_hours * 60))

                eta_h = eta_minutes // 60

                eta_m = eta_minutes % 60

                if eta_h > 0:

                    console.print(f"ETA to daily cap: ~{eta_h}h {eta_m}m")

                else:

                    console.print(f"ETA to daily cap: ~{eta_m}m")

    if evt.get("warmup_enabled"):

        wm = evt.get("warmup_minutes", "-")

        ws = evt.get("warmup_start_multiplier", "-")

        we = evt.get("warmup_end_multiplier", "-")

        console.print(f"Warm-up: enabled | {wm}m | {ws}x -> {we}x")

    if evt.get("adaptive_backoff_enabled"):

        console.print("Adaptive backoff: enabled")

    console.print(

        "Schedule: "

        + _format_schedule(

            sched_days,

            sched_windows,

            sleep_start,

            sleep_end,

            evt.get("schedule_windows_weekday", getattr(ad, "schedule_windows_weekday", None)),

            evt.get("schedule_windows_weekend", getattr(ad, "schedule_windows_weekend", None)),

        )

    )

    console.print("Press Ctrl+C to stop and return to menu.")

    console.print()





def _status_callback_factory(

    ad: Campaign,

    *,

    dry: bool,

    bot_mgr: TelegramBotManager | None = None,

    bot_cfg: Optional[dict] = None,

    settings: Optional[AdvancedSettings] = None,

    suppress_updates_fn: Optional[callable] = None,

    event_sink: Optional[callable] = None,

    notify_start: bool = True,

) -> Tuple[Dict[str, Any], Any]:

    ui: Dict[str, Any] = {

        "sent_total": 0,

        "next_in_sec": None,

        "last_target": None,

        "last_link": None,

        "started_block_printed": False,

        "last_wait_print_sec": None,

        "last_action": None,

        "bot_sent_count": 0,

    }

    alert_mode = (bot_cfg or {}).get("alert_mode", "errors")

    alert_every = int((bot_cfg or {}).get("alert_every_n", 10) or 10)

    if getattr(ad, "bot_alert_mode", None):

        alert_mode = str(ad.bot_alert_mode)

    if getattr(ad, "bot_alert_every_n", None):

        alert_every = int(ad.bot_alert_every_n)



    def _notify(msg: str) -> None:

        if bot_mgr is None:

            return

        if settings and getattr(settings, "bot_quiet_hours_enabled", False):

            now = datetime.now()

            if not is_allowed_now(

                now,

                days_mode="all",

                windows=None,

                sleep_start=getattr(settings, "bot_quiet_start", ""),

                sleep_end=getattr(settings, "bot_quiet_end", ""),

            ):

                return

        try:

            asyncio.get_running_loop().create_task(bot_mgr.send_message(msg))

        except Exception:

            return



    def _push_event(line: str) -> None:

        if event_sink is None:

            return

        try:

            event_sink(line)

        except Exception:

            return



    def _is_suppressed() -> bool:

        try:

            return bool(suppress_updates_fn()) if suppress_updates_fn else False

        except Exception:

            return False



    def on_event(evt: Dict[str, Any]) -> None:

        try:

            et = evt.get("type", "unknown")



            if et == "start" and not ui["started_block_printed"]:

                _print_ad_header_block(ad, dry=dry, evt=evt)

                ui["started_block_printed"] = True

                if notify_start and alert_mode in ("every", "summary", "errors"):
                    _notify(f"\U0001F680 Ad started\nAd: {ad.name}\nMode: {'DRY' if dry else 'LIVE'}")


            if evt.get("sent_total") is not None:

                try:

                    ui["sent_total"] = int(evt.get("sent_total"))

                except Exception:

                    pass



            if evt.get("target"):

                ui["last_target"] = evt.get("target")

            if evt.get("link"):

                ui["last_link"] = evt.get("link")



            if evt.get("next_in_sec") is not None:

                try:

                    ui["next_in_sec"] = int(evt.get("next_in_sec"))

                except Exception:

                    ui["next_in_sec"] = None

            elif evt.get("next_at") is not None:

                na = _safe_parse_next_at(evt.get("next_at"))

                if isinstance(na, datetime):

                    diff = int((na - datetime.now()).total_seconds())

                    ui["next_in_sec"] = max(0, diff)



            ts = datetime.now().strftime("%H:%M:%S %d/%m/%Y")
            group, topic = _split_target_label(ui["last_target"])

            link = ui["last_link"] or "-"

            next_txt = _fmt_next(ui["next_in_sec"])

            total = ui["sent_total"]



            if et == "wait":

                # Keep live panel updated without console spam.

                now_ts = time.time()

                last_wait = ui.get("last_wait_print_sec")

                wait_every = int(getattr(settings, "live_update_every_sec", 10) or 10) if settings else 10

                if last_wait is None or now_ts - last_wait >= wait_every:

                    ui["last_wait_print_sec"] = now_ts

                    _push_event(f"{ts} | {ad.name} | waiting | next in {next_txt} | sent: {total}")

                return



            if et == "schedule_wait":

                _push_event(f"{ts} | {ad.name} | schedule wait | next in {next_txt} | sent: {total}")

                return

            if et == "slowmode_wait":
                location = group if topic == "-" else f"{group} -> {topic}"
                _push_event(
                    f"{ts} | {ad.name} | slow mode in {location}; deferred for {next_txt} | sent: {total}"
                )
                return

            if et == "destination_wait":
                location = group if topic == "-" else f"{group} -> {topic}"
                _push_event(
                    f"{ts} | {ad.name} | Telegram wait in {location}; "
                    f"skipped until {next_txt} while other destinations continue | sent: {total}"
                )
                return

            if et == "selected":

                location = group if topic == "-" else f"{group} -> {topic}"

                _push_event(f"{ts} | {ad.name} | selected {location} | link: {link}")

                return



            if et in ("sent", "dry_send", "flood_wait", "batch_gap", "cooldown_all"):

                ui["last_action"] = et

                if et == "sent":

                    ui["bot_sent_count"] += 1

                    if alert_mode == "every":

                        location = group if topic == "-" else f"{group} -> {topic}"

                        link_txt = link if link != "-" else "(no link)"

                        _notify(
                            f"\u2705 Message delivered\nAd: {ad.name}\nTarget: {location}\nLink: {link_txt}\nTotal sent: {ui['sent_total']}"
                        )
                    elif alert_mode == "summary" and ui["bot_sent_count"] % max(1, alert_every) == 0:

                        _notify(f"\U0001F4C8 Progress update\nAd: {ad.name}\nSent so far: {ui['bot_sent_count']}")
                    location = group if topic == "-" else f"{group} -> {topic}"

                    _push_event(f"{ts} | {ad.name} | sent in {location} | sent: {total}")

                elif et == "dry_send":

                    location = group if topic == "-" else f"{group} -> {topic}"

                    _push_event(f"{ts} | {ad.name} | dry_send in {location} | sent: {total}")

                if et == "flood_wait":

                    _notify(f"\u23F3 Flood wait\nAd: {ad.name}")
                    _push_event(f"{ts} | {ad.name} | flood_wait | sent: {total}")

                if et == "batch_gap":

                    _push_event(f"{ts} | {ad.name} | batch gap | sent: {total}")

                if et == "cooldown_all":

                    _push_event(f"{ts} | {ad.name} | cooldown all | next in {next_txt} | sent: {total}")

                return



            if et == "next":

                action = ui.get("last_action") or "sent"

                action_txt = "sent" if action in ("sent", "dry_send") else action.replace("_", " ")

                location = group if topic == "-" else f"{group} -> {topic}"

                if not _is_suppressed():

                    console.print(

                        f"{ts} | {ad.name} | {action_txt} in {location} | next in {next_txt} | sent so far: {total}"

                    )

                _push_event(f"{ts} | {ad.name} | {action_txt} in {location} | next in {next_txt} | sent: {total}")

                return



            if et == "error":

                extra = evt.get("error") or "-"

                location = group if topic == "-" else f"{group} -> {topic}"

                if not _is_suppressed():

                    console.print(f"{ts} | {ad.name} | error in {location} | {extra} | sent so far: {total}")

                link_txt = link if link != "-" else "(no link)"

                _notify(f"\u26A0\ufe0f Send failed\nAd: {ad.name}\nTarget: {location}\nLink: {link_txt}\nReason: {extra}")
                _push_event(f"{ts} | {ad.name} | error in {location} | {extra} | sent: {total}")

                return

            if et == "target_removed":
                extra = str(evt.get("error") or "The destination is no longer usable.")
                action = str(evt.get("action") or "Removed automatically from this ad.")
                remaining = int(evt.get("targets_remaining") or 0)
                location = group if topic == "-" else f"{group} -> {topic}"
                _notify(
                    f"🗑️ Destination removed\n\n"
                    f"Ad: {ad.name}\n"
                    f"Destination: {location}\n"
                    f"Action: {action}\n"
                    f"Reason: {extra}\n"
                    f"Destinations remaining: {remaining}"
                )
                _push_event(f"{ts} | {ad.name} | removed {location} | {extra} | remaining: {remaining}")
                return

            if et == "source_error":
                source = str(evt.get("source") or "Unknown source")
                extra = str(evt.get("error") or "The source could not be read.")
                _notify(
                    f"⚠️ Source unavailable\n\n"
                    f"Ad: {ad.name}\n"
                    f"Source: {source}\n"
                    f"Reason: {extra}"
                )
                _push_event(f"{ts} | {ad.name} | source unavailable | {source} | {extra}")
                return



            if et == "stop":
                extra = evt.get("info") or "-"
                reason = _friendly_stop_reason(str(extra))
                if not _is_suppressed():
                    console.print(f"{ts} | {ad.name} | stopped | {reason} | sent so far: {total}")
                if alert_mode in ("every", "summary", "errors"):
                    _notify(f"\u23F9\ufe0f Ad stopped\nAd: {ad.name}\nReason: {reason}")
                _push_event(f"{ts} | {ad.name} | stopped | {reason} | sent: {total}")
                return


            if et == "paused":

                if not _is_suppressed():

                    console.print(f"{ts} | {ad.name} | paused | sent: {total}")

                _push_event(f"{ts} | {ad.name} | paused | sent: {total}")

                return



        except Exception as e:

            console.print(f"[red]UI callback error:[/red] {type(e).__name__}: {e}")



    return ui, on_event





async def run_app() -> None:
    ensure_folders()
    try:
        loop = asyncio.get_running_loop()
        if hasattr(loop, "set_exception_handler"):
            def _loop_exception_handler(loop, context):
                exc = context.get("exception")
                if isinstance(exc, AttributeError):
                    msg = str(exc)
                    if "shutdown" in msg and "NoneType" in msg:
                        return
                loop.default_exception_handler(context)
            loop.set_exception_handler(_loop_exception_handler)
    except Exception:
        pass

    # Global single-instance lock for the whole app lifetime.
    app_lock = None

    try:

        app_lock = acquire_lock(LOCKS_DIR / "telegram_forwarder.lock")

    except Exception as e:

        console.print(f"[red]{e}[/red]")

        return



    tg: TgClient | None = None

    bot_mgr: TelegramBotManager | None = None

    running_tasks: Dict[str, Dict[str, Any]] = {}

    suppress_updates = True

    bot_lock = None

    main_loop = asyncio.get_running_loop()

    settings = load_settings()

    save_settings(settings)



    def _loop_exception_handler(loop, context) -> None:

        exc = context.get("exception")

        msg = str(exc) if exc else context.get("message", "")

        if "NoneType" in msg and "shutdown" in msg and "proactor" in (context.get("message", "").lower()):

            return

        if exc and "NoneType" in str(exc) and "shutdown" in str(exc):

            return

        loop.default_exception_handler(context)



    try:

        main_loop.set_exception_handler(_loop_exception_handler)

    except Exception:

        pass



    try:

        print_header()

        _print_last_run()



        # maintenance: logs/history/auto-export

        def _cleanup_logs(retention_days: Optional[int]) -> None:

            if retention_days is None or retention_days <= 0:

                return

            cutoff = datetime.now() - timedelta(days=int(retention_days))

            try:

                for p in LOGS_DIR.glob("ad_*.log"):

                    try:

                        if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:

                            p.unlink()

                    except Exception:

                        pass

            except Exception:

                pass



        async def _cleanup_history(retention_days: Optional[int]) -> None:

            if retention_days is None or retention_days <= 0:

                return

            try:

                history = await get_history()

                await history.cleanup_older_than(retention_days)

            except Exception:

                pass



        async def _auto_export_loop() -> None:

            hours = getattr(settings, "auto_export_hours", None)

            if hours is None or hours <= 0:

                return

            while True:

                try:

                    last = getattr(settings, "last_auto_export_at", None)

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

                            with open(filepath, "w", encoding="utf-8") as f:

                                f.write(

                                    "Timestamp,Ad ID,Ad Name,Message Link,Group ID,Group Title,Topic ID,Topic Title,"

                                    "Success,Error Type,Error Message,Stars Cost\n"

                                )

                                for r in records:

                                    f.write(f'"{r["timestamp"]}",')

                                    f.write(f'"{r["ad_id"]}",')

                                    f.write(f'"{r["campaign_name"]}",')
                                    f.write(f'"{r["message_link"]}",')

                                    f.write(f'"{r["group_id"]}",')

                                    f.write(f'"{r["group_title"]}",')

                                    f.write(f'"{r.get("topic_id", "")}",')

                                    f.write(f'"{r.get("topic_title", "")}",')

                                    f.write(f'"{r["success"]}",')

                                    f.write(f'"{r.get("error_type", "")}",')

                                    f.write(f'"{r.get("error_message", "")}",')

                                    f.write(f'"{r.get("stars_cost", 0)}"\n')

                            update_last_export(settings, now)

                    except Exception:

                        pass

                await asyncio.sleep(60)



        _cleanup_logs(getattr(settings, "log_retention_days", None))

        asyncio.get_running_loop().create_task(_cleanup_history(getattr(settings, "history_retention_days", None)))

        asyncio.get_running_loop().create_task(_auto_export_loop())



        def _ensure_bot_lock() -> bool:

            nonlocal bot_lock

            if bot_lock is not None:

                return True

            try:

                bot_lock = acquire_lock(LOCKS_DIR / "telegram_forwarder_bot.lock")

                return True

            except Exception as e:

                console.print(f"[yellow]{e}[/yellow]")

                return False



        async def _force_bot_takeover(token: str, chat_id: str) -> bool:

            nonlocal bot_mgr

            try:

                if bot_mgr is not None:

                    try:

                        bot_mgr.stop_background()

                    except Exception:

                        pass

                    _release_bot_lock()

                    bot_mgr = None

                bot_mgr = TelegramBotManager(token, chat_id, control=bot_control)

                if not _ensure_bot_lock():

                    _release_bot_lock()

                    return False

                bot_mgr.start_background()

                return True

            except Exception as e:

                console.print(f"[red]Force takeover failed:[/red] {e}")

                _release_bot_lock()

                bot_mgr = None

                return False



        def _release_bot_lock() -> None:

            nonlocal bot_lock

            if bot_lock is None:

                return

            try:

                bot_lock.release()

            except Exception:

                pass

            bot_lock = None



        async def _start_ad_background(

            c: Campaign,

            *,

            dry: bool,

            allow_risky: bool,

            suppress_updates_fn: Optional[callable] = None,

        ) -> tuple[bool, str]:

            nonlocal tg

            if tg is None:

                return False, "Not logged in."

            if c.id in running_tasks:

                return False, "Ad already running."



            event_buf = deque(maxlen=200)

            if suppress_updates_fn is None:
                def suppress_updates_fn() -> bool:
                    return False



            async def _runner() -> None:

                try:

                    await _run_campaign_flow(

                        tg=tg,

                        dry=dry,

                        bot_mgr=bot_mgr,

                        suppress_updates_fn=suppress_updates_fn,

                        campaign=c,

                        event_sink=event_buf.append,

                        interactive=False,

                        allow_risky=allow_risky,

                        show_risk=False,

                        settings=settings,

                    )

                except Exception as e:

                    console.print(f"[red]Runner error:[/red] {e}")

                finally:

                    running_tasks.pop(c.id, None)



            task = asyncio.create_task(_runner())

            running_tasks[c.id] = {

                "task": task,

                "name": c.name,

                "dry": dry,

                "started_at": datetime.now(),

                "events": event_buf,

            }

            return True, "started"



        def _preflight_ad(c: Campaign, *, dry: bool) -> tuple[bool, bool]:

            allow_risky = False
            skip_schedule_prompt = False



            if c.id in running_tasks:

                ans = input(

                    "Ad already running. Wait (w) or force restart now... (w/F): "

                ).strip().lower()

                if ans != "f":

                    return False, False

                info = running_tasks.get(c.id) or {}

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                try:

                    stop_state(state_path, c.id)

                except Exception:

                    pass

                task = info.get("task")

                if task:

                    try:

                        task.cancel()

                    except Exception:

                        pass

                running_tasks.pop(c.id, None)

                st = load_state(state_path, c.id)

                if st is not None:

                    st.paused = False

                    st.paused_at = None

                    st.stopped = False

                    st.stopped_at = None

                    st.next_at = None

                    save_state(state_path, st)

                console.print("[yellow]Previous run stopped. Starting fresh...[/yellow]")



            if not getattr(c, "enabled", True):

                ans = input("This ad is disabled. Enable and run now... (y/N): ").strip().lower()

                if ans != "y":

                    return False, False

                c.enabled = True

                replace_campaign(c)



            state_path = PROFILES_DIR / f"state_{c.id}.json"

            st = load_state(state_path, c.id)

            if st is not None:

                if getattr(st, "paused", False):

                    ans = input("This Ad is PAUSED. Resume now... (y/N): ").strip().lower()

                    if ans != "y":

                        return False, False

                    resume_state(state_path, c.id)

                if getattr(st, "stopped", False):
                    parsed = _safe_parse_next_at(getattr(st, "next_at", None))
                    sched_txt = parsed.strftime("%H:%M:%S %d/%m/%Y") if isinstance(parsed, datetime) else "not set"
                    ans = input(
                        f"This Ad is STOPPED. Resume schedule ({sched_txt}) [Enter], start now [S], cancel [C]: "
                    ).strip().lower()
                    if ans in ("c", "cancel", "n", "no"):
                        return False, False
                    st.stopped = False
                    st.stopped_at = None
                    st.paused = False
                    st.paused_at = None
                    if ans in ("s", "start", "now", "y"):
                        st.next_at = None
                    save_state(state_path, st)
                    skip_schedule_prompt = True

                parsed = _safe_parse_next_at(getattr(st, "next_at", None))

                if not skip_schedule_prompt and parsed and parsed > datetime.now():

                    ans = input(
                        f"Next scheduled at {parsed.strftime('%H:%M:%S %d/%m/%Y')}. "
                        "Resume schedule (keep cooldown) [Enter], start now [S]: "
                    ).strip().lower()

                    if ans in ("s", "start", "now", "y"):

                        st.next_at = None

                        save_state(state_path, st)



            risk = assess_campaign_risk(c)

            console.print(

                f"[bold]Risk level:[/bold] {risk.level.upper()} (score {risk.score})"

            )

            for r in risk.reasons:

                console.print(f"[yellow]- {r}[/yellow]")

            if risk.guardrails:

                console.print("[red]Guardrails triggered:[/red]")

                for g in risk.guardrails:

                    console.print(f"[red]- {g}[/red]")

                ans = input("Proceed anyway... (y/N): ").strip().lower()

                if ans != "y":

                    return False, False

                allow_risky = True

            elif risk.level == "high":

                ans = input("High risk detected. Proceed anyway... (y/N): ").strip().lower()

                if ans != "y":

                    return False, False

                allow_risky = True



            return True, allow_risky



        async def _in_main(coro):

            if asyncio.get_running_loop() is main_loop:

                return await coro

            fut = asyncio.run_coroutine_threadsafe(coro, main_loop)

            return await asyncio.wrap_future(fut)



        async def _ctrl_list_running() -> str:
            async def _impl() -> str:
                if not running_tasks:
                    return "Running ads\n\nNo ads are running."
                lines = ["\U0001F3C3 Running ads"]
                for cid, info in running_tasks.items():
                    mode = "DRY" if info.get("dry") else "LIVE"
                    started_at = info.get("started_at")
                    started_txt = started_at.strftime("%H:%M:%S %d/%m/%Y") if isinstance(started_at, datetime) else "-"
                    lines.append(f"- {info.get('name')} | Mode: {mode} | Started: {started_txt}")
                return "\n".join(lines)
            return await _in_main(_impl())


        async def _ctrl_stop_running(ad_id: str) -> str:
            async def _impl() -> str:
                info = running_tasks.get(ad_id)
                if not info:
                    return f"Ad not running\nAd ID: {ad_id}"
                state_path = PROFILES_DIR / f"state_{ad_id}.json"
                stop_state(state_path, ad_id)
                task = info.get("task")
                if task:
                    task.cancel()
                running_tasks.pop(ad_id, None)
                name = info.get("name") or ad_id
                return f"Stop requested\nAd: {name}"
            return await _in_main(_impl())


        async def _ctrl_start_ad(ad_id: str, dry: bool, force: bool) -> str:

            async def _impl() -> str:

                nonlocal tg

                if tg is None:
                    return "Not logged in. Please log in first."
                c = get_campaign(ad_id)
                if not c:
                    return f"Ad not found\nAd ID: {ad_id}"
                if c.id in running_tasks:
                    return f"Ad already running\nAd: {c.name}"

                if not getattr(c, "enabled", True):
                    if not force:
                        return "Ad is disabled. Use /menu and choose a FORCE action to override."
                    c.enabled = True
                    replace_campaign(c)


                state_path = PROFILES_DIR / f"state_{c.id}.json"

                st = load_state(state_path, c.id)

                scheduled_for: Optional[datetime] = None
                if st is not None:

                    paused = bool(getattr(st, "paused", False))

                    stopped = bool(getattr(st, "stopped", False))

                    next_at = _safe_parse_next_at(getattr(st, "next_at", None))

                    if (paused or stopped) and not force:
                        parts = []
                        if paused:
                            parts.append("paused")
                        if stopped:
                            parts.append("stopped")
                        return "Ad is not ready to start: " + ", ".join(parts) + ". Use /menu and choose a FORCE action to override."

                    if next_at and next_at > datetime.now() and not force:
                        scheduled_for = next_at

                    if force:

                        if paused:

                            st.paused = False

                            st.paused_at = None

                        if stopped:

                            st.stopped = False

                            st.stopped_at = None

                        st.next_at = None

                        save_state(state_path, st)



                risk = assess_campaign_risk(c)

                if (risk.guardrails or risk.level == "high") and not force:
                    return "Risk guardrails triggered. Use /menu and choose a FORCE action to override."


                ok, msg = await _start_ad_background(

                    c,

                    dry=dry,

                    allow_risky=force,

                    suppress_updates_fn=lambda: True,

                )

                if not ok:
                    return f"Start failed: {msg}"
                mode_txt = "DRY" if dry else "LIVE"
                if scheduled_for:
                    return (
                        f"\u2705 Start command sent\nAd: {c.name}\nMode: {mode_txt}\n"
                        f"Next send: {scheduled_for.strftime('%H:%M:%S %d/%m/%Y')}"
                    )
                return f"\u2705 Start command sent\nAd: {c.name}\nMode: {mode_txt}"
            return await _in_main(_impl())


        async def _ctrl_resume_schedule(ad_id: str) -> str:

            async def _impl() -> str:

                nonlocal tg

                if tg is None:
                    return "Not logged in. Please log in first."
                c = get_campaign(ad_id)
                if not c:
                    return f"Ad not found\nAd ID: {ad_id}"
                if c.id in running_tasks:
                    return f"Ad already running\nAd: {c.name}"
                if not getattr(c, "enabled", True):
                    return "Ad is disabled. Enable it first."

                state_path = PROFILES_DIR / f"state_{c.id}.json"
                st = load_state(state_path, c.id)
                if st is None:
                    return "No saved schedule found for this ad. Use Start to begin immediately."

                next_at = _safe_parse_next_at(getattr(st, "next_at", None))
                if not isinstance(next_at, datetime) or next_at <= datetime.now():
                    return "No future schedule found for this ad. Use Start to begin immediately."

                if bool(getattr(st, "paused", False)):
                    st.paused = False
                    st.paused_at = None
                if bool(getattr(st, "stopped", False)):
                    st.stopped = False
                    st.stopped_at = None

                st.next_at = next_at
                save_state(state_path, st)

                risk = assess_campaign_risk(c)
                if risk.guardrails or risk.level == "high":
                    return "Risk guardrails triggered. Use /menu and choose a FORCE action to override."

                ok, msg = await _start_ad_background(
                    c,
                    dry=False,
                    allow_risky=False,
                    suppress_updates_fn=lambda: True,
                )

                if not ok:
                    return f"Start failed: {msg}"

                return (
                    f"\u2705 Resume scheduled\nAd: {c.name}\n"
                    f"Next send: {next_at.strftime('%H:%M:%S %d/%m/%Y')}"
                )

            return await _in_main(_impl())


        async def _ctrl_ad_status(ad_id: str) -> str:

            async def _impl() -> str:

                c = get_campaign(ad_id)

                if not c:
                    return f"Ad not found\nAd ID: {ad_id}"
                state_path = PROFILES_DIR / f"state_{c.id}.json"
                st = load_state(state_path, c.id)
                running = "yes" if c.id in running_tasks else "no"
                enabled = "yes" if getattr(c, "enabled", True) else "no"
                paused = "yes" if st and getattr(st, "paused", False) else "no"
                stopped = "yes" if st and getattr(st, "stopped", False) else "no"
                sent_total = getattr(st, "sent_total", 0) if st else 0
                na = _safe_parse_next_at(getattr(st, "next_at", None)) if st else None
                next_txt = na.strftime("%H:%M:%S %d/%m/%Y") if isinstance(na, datetime) else "-"
                per_hour, per_day = _estimate_send_rates(
                    send_gap_min=c.send_gap_min_sec,

                    send_gap_max=c.send_gap_max_sec,

                    batch_gap_min=c.batch_gap_min_sec,

                    batch_gap_max=c.batch_gap_max_sec,

                    batch_size=max(1, len(c.target_refs)),

                )

                progress_line = ""

                eta_line = ""

                daily_cap = getattr(c, "daily_cap", None)

                if isinstance(daily_cap, int) and daily_cap > 0:

                    sent_today = int(getattr(st, "day_sent_count", 0) or 0) if st else 0

                    remaining = max(0, int(daily_cap) - sent_today)

                    progress_line = f"\nprogress_today: {sent_today}/{daily_cap} (remaining {remaining})"

                    if per_hour > 0 and remaining > 0:

                        eta_hours = remaining / float(per_hour)

                        eta_minutes = int(round(eta_hours * 60))

                        eta_h = eta_minutes // 60

                        eta_m = eta_minutes % 60

                        if eta_h > 0:

                            eta_line = f"\neta_daily_cap: ~{eta_h}h {eta_m}m"

                        else:

                            eta_line = f"\neta_daily_cap: ~{eta_m}m"

                return (
                    f"Ad status\n"
                    f"Ad: {c.name}\n"
                    f"Enabled: {enabled}\n"
                    f"Running: {running}\n"
                    f"Paused: {paused}\n"
                    f"Stopped: {stopped}\n"
                    f"Total sent: {sent_total}\n"
                    f"Next send: {next_txt}\n"
                    f"Estimated rate: {per_hour:.1f}/hour | {per_day:.1f}/24h" + f"{progress_line}{eta_line}"
                )
            return await _in_main(_impl())


        async def _ctrl_enable_ad(ad_id: str) -> str:

            async def _impl() -> str:

                c = get_campaign(ad_id)
                if not c:
                    return f"Ad not found\nAd ID: {ad_id}"
                if getattr(c, "enabled", True):
                    return f"Ad already enabled\nAd: {c.name}"
                c.enabled = True
                replace_campaign(c)
                return f"Ad enabled\nAd: {c.name}"
            return await _in_main(_impl())


        async def _ctrl_disable_ad(ad_id: str) -> str:

            async def _impl() -> str:

                c = get_campaign(ad_id)
                if not c:
                    return f"Ad not found\nAd ID: {ad_id}"
                c.enabled = False
                replace_campaign(c)
                info = running_tasks.get(c.id)
                if info:
                    state_path = PROFILES_DIR / f"state_{c.id}.json"
                    stop_state(state_path, c.id)
                    task = info.get("task")
                    if task:
                        task.cancel()
                    running_tasks.pop(c.id, None)
                    return f"Ad disabled and stopped\nAd: {c.name}"
                return f"Ad disabled\nAd: {c.name}"
            return await _in_main(_impl())


        async def _ctrl_health() -> str:

            async def _impl() -> str:

                logged_in = "yes" if tg and tg.client.is_connected() else "no"
                running_count = len(running_tasks)
                return (
                    "System health\n\n"
                    f"\U0001F510 Logged in: {logged_in}\n"
                    f"\U0001F3C3 Running ads: {running_count}\n"
                    "\U0001F916 Bot: online"
                )
            return await _in_main(_impl())


        async def _ctrl_list_disabled(ad_id: str) -> str:

            async def _impl() -> str:

                c = get_campaign(ad_id)

                if not c:

                    return f" Ad not found: {ad_id}"

                header, lines = _format_disabled_targets(c)

                if not lines:

                    return f" {header}\n\nNone."

                return " " + header + "\n\n" + "\n".join(lines)

            return await _in_main(_impl())



        async def _ctrl_clear_disabled(ad_id: str) -> str:

            async def _impl() -> str:

                c = get_campaign(ad_id)

                if not c:

                    return f" Ad not found: {ad_id}"

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                st = load_state(state_path, c.id)

                if st is None:

                    return "...... No state found for this ad."

                return " Disabled targets cleared."

            return await _in_main(_impl())



        bot_control = BotControl(

            list_running=_ctrl_list_running,

            stop_running=_ctrl_stop_running,

            start_ad=_ctrl_start_ad,
            resume_schedule=_ctrl_resume_schedule,

            ad_status=_ctrl_ad_status,

            enable_ad=_ctrl_enable_ad,

            disable_ad=_ctrl_disable_ad,

            health=_ctrl_health,

            list_disabled=_ctrl_list_disabled,

            clear_disabled=_ctrl_clear_disabled,

        )



        first_menu = True

        skip_pause_once = False

        async def _input_async(prompt: str = "") -> str:

            return await asyncio.to_thread(input, prompt)



        while True:

            if not first_menu and not skip_pause_once:

                await _input_async("\nPress Enter to return to menu...")

            skip_pause_once = False

            first_menu = False



            if not running_tasks:

                suppress_updates = True

            choice = await asyncio.to_thread(main_menu)



            if choice == "1":

                console.print("[bold]First-time setup[/bold]")

                api_id = int(input("Enter API ID: ").strip())

                api_hash = input("Enter API HASH: ").strip()

                phone = input("Enter phone number (with +): ").strip()

                save_config(AppConfig(api_id=api_id, api_hash=api_hash, phone=phone))

                console.print("[green]Saved to data/config.json[/green]")



            elif choice == "2":

                accounts = list_accounts()

                if accounts:

                    active = get_active_account()

                    if active is None:

                        _print_accounts()

                        pick = input("Set active account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        if not set_active_account(int(pick)):

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        active = get_active_account()

                    if active is None:

                        console.print("[yellow]No active account selected.[/yellow]")

                        continue

                    proxy = pick_proxy_for_account(active, rotate=True)

                    if proxy is None:

                        has_pool = len(list_account_proxies(active.id)) > 0

                        if has_pool or (active.proxy_host and active.proxy_port):

                            console.print("[yellow]Proxy set but pysocks missing; proceeding without proxy.[/yellow]")

                    creds = TgCredentials(api_id=active.api_id, api_hash=active.api_hash, phone=active.phone)

                    tg = TgClient(creds, session_name=f"acct_{active.id}", proxy=proxy)

                else:

                    cfg = load_config()

                    if not cfg:

                        console.print("[red]No config found. Run option 1 first.[/red]")

                        continue

                    tg = TgClient(TgCredentials(api_id=cfg.api_id, api_hash=cfg.api_hash, phone=cfg.phone))

                await tg.connect_and_login()

                console.print("[green]Logged in successfully.[/green]")

                cfg_bot = load_bot_config()

                if cfg_bot and cfg_bot.get("enabled", False) and cfg_bot.get("auto_start_on_login", True):

                    if bot_mgr is None or bot_mgr.app is None:

                        if not _ensure_bot_lock():

                            console.print("[yellow]Bot already running in another instance. Attempting takeover...[/yellow]")

                            ok = await _force_bot_takeover(cfg_bot["token"], cfg_bot["chat_id"])

                            console.print("[green]Bot takeover complete.[/green]" if ok else "[yellow]Bot takeover failed.[/yellow]")

                        else:

                            bot_mgr = TelegramBotManager(cfg_bot["token"], cfg_bot["chat_id"], control=bot_control)

                            try:

                                bot_mgr.start_background()

                                console.print("[green]Bot started (auto).[/green]")

                                # Bot now announces its own online status and alert settings.
                            except Exception as e:

                                console.print(f"[red]Failed to start bot:[/red] {e}")

                                _release_bot_lock()

                                bot_mgr = None

                elif cfg_bot and cfg_bot.get("enabled", False):
                    if bot_mgr is not None and bot_mgr.app is not None:
                        # No extra login message; keep bot announcements single-source.
                        pass


            elif choice == "3":

                if tg is None:

                    console.print("[yellow]Not logged in.[/yellow]")

                else:

                    await tg.close()

                    tg = None

                    console.print("[green]Logged out.[/green]")



            elif choice == "4":

                if tg is None:

                    console.print("[red]Please login first (option 2).[/red]")

                    continue

                console.print("Syncing destinations...")

                dests = await _retry_if_db_locked(lambda: sync_destinations(tg.client))

                save_json(DESTINATIONS_CACHE, [d.__dict__ for d in dests])

                console.print(f"[green]Saved {len(dests)} destinations to cache.[/green]")

                render_destinations(dests)



            elif choice == "5":

                if tg is None:

                    console.print("[red]Please login first (option 2).[/red]")

                    continue

                cached = load_json(DESTINATIONS_CACHE, default=[])

                if not cached:

                    console.print("[yellow]No cached destinations yet. Use option 4.[/yellow]")

                    continue



                dests = [Destination(**d) for d in cached]

                forum_dests = [d for d in dests if d.kind == "group" and getattr(d, "is_forum", False)]

                if not forum_dests:

                    console.print("[yellow]No forum groups (topics) detected.[/yellow]")

                    continue



                console.print("[bold]Forum groups[/bold]")

                for i, d in enumerate(forum_dests, start=1):

                    console.print(f"{i}) {d.title}  Stars: {_stars_txt(d)}")



                sel = input("Pick a group number to view topics: ").strip()

                if not sel.isdigit():

                    console.print("[yellow]Invalid input.[/yellow]")

                    continue

                idx = int(sel)

                if idx < 1 or idx > len(forum_dests):

                    console.print("[yellow]Out of range.[/yellow]")

                    continue



                group = forum_dests[idx - 1]

                console.print(f"Loading topics for: [bold]{group.title}[/bold]  (Stars: {_stars_txt(group)})")



                try:

                    topics = await _retry_if_db_locked(lambda: fetch_forum_topics(tg.client, group.id, limit=200))

                except Exception as e:

                    console.print(f"[red]Failed to load topics:[/red] {e}")

                    continue



                if not topics:

                    console.print("[yellow]No topics returned for this group.[/yellow]")

                    continue



                console.print("[bold]Topics[/bold]")

                for j, t in enumerate(topics, start=1):

                    console.print(f"{j}) {t.title}")



            elif choice == "6":

                if tg is None:

                    console.print("[red]Please login first (option 2).[/red]")

                    continue

                cached = load_json(DESTINATIONS_CACHE, default=[])

                if not cached:

                    console.print("[yellow]No cached destinations yet. Use option 4 first.[/yellow]")

                    continue



                dests = [Destination(**d) for d in cached]

                groups = [d for d in dests if d.kind == "group"]

                if not groups:

                    console.print("[yellow]No groups found in cached destinations.[/yellow]")

                    continue



                stars_map = _load_stars_map()



                console.print("[bold]Groups[/bold]")

                for i, g in enumerate(groups, start=1):

                    forum_flag = "" if getattr(g, "is_forum", False) else ""

                    stars_cost = stars_map.get(g.id, None)

                    stars_tag = f"stars {stars_cost}" if isinstance(stars_cost, int) and stars_cost > 0 else ""

                    console.print(f"{i}) {g.title} {forum_flag}  {stars_tag}".rstrip())



                pick = input("Pick a group number to create targets (or 'b' to go back): ").strip()

                if _is_back(pick):

                    continue

                if not pick.isdigit():

                    console.print("[yellow]Invalid input.[/yellow]")

                    continue



                gi = int(pick)

                if gi < 1 or gi > len(groups):

                    console.print("[yellow]Out of range.[/yellow]")

                    continue



                group = groups[gi - 1]

                stars_cost = stars_map.get(group.id, None)

                is_paid = isinstance(stars_cost, int) and stars_cost > 0



                if is_paid:

                    console.print(f"[yellow]Warning:[/yellow] This group requires Stars per message:  {stars_cost}")

                    console.print("[yellow]Targets created for this group will be marked as PAID.[/yellow]")



                if not getattr(group, "is_forum", False):

                    new_targets = [

                        DestinationTarget(

                            group_id=group.id,

                            group_title=group.title,

                            topic_id=None,

                            topic_title=None,

                            topic_top_message=None,

                            paid_message_stars=stars_cost if is_paid else None,

                            is_paid=is_paid,

                        )

                    ]

                    existing = load_targets()

                    merged = add_targets(existing, new_targets)

                    save_targets(merged)

                    console.print("[green]Saved 1 destination target (no topic).[/green]")

                    continue



                console.print(f"Loading topics for: [bold]{group.title}[/bold]  (Stars: {_stars_txt(group)})")

                try:

                    topics = await _retry_if_db_locked(lambda: fetch_forum_topics(tg.client, group.id, limit=200))

                except Exception as e:

                    console.print(f"[red]Failed to load topics:[/red] {e}")

                    continue



                if not topics:

                    console.print("[yellow]No topics returned for this group.[/yellow]")

                    continue



                console.print("[bold]Topics[/bold]")

                for j, t in enumerate(topics, start=1):

                    console.print(f"{j}) {t.title}")



                raw = input("Select topics (example 1,3,5-8 or all). 'b' to go back: ").strip()

                if _is_back(raw):

                    continue

                idxs = parse_selection(raw, max_index=len(topics))

                if not idxs:

                    console.print("[yellow]No topics selected.[/yellow]")

                    continue



                new_targets: list[DestinationTarget] = []

                for j in idxs:

                    t = topics[j - 1]

                    new_targets.append(

                        DestinationTarget(

                            group_id=group.id,

                            group_title=group.title,

                            topic_id=t.topic_id,

                            topic_title=t.title,

                            topic_top_message=t.top_message,

                            paid_message_stars=stars_cost if is_paid else None,

                            is_paid=is_paid,

                        )

                    )



                existing = load_targets()

                merged = add_targets(existing, new_targets)

                save_targets(merged)



                console.print(f"[green]Saved {len(new_targets)} destination targets for this group.[/green]")



            elif choice == "7":

                targets = load_targets()

                if not targets:

                    console.print("[yellow]No destination targets saved yet. Use option 6.[/yellow]")

                    continue

                _print_targets(targets)



            elif choice == "9":

                targets = load_targets()

                if not targets:

                    console.print("[yellow]No destination targets saved. Use option 6 first.[/yellow]")

                    continue



                console.print(f"[bold]Targets available:[/bold] total={len(targets)}")



                name = input("Ad name (or 'b' to go back): ").strip()

                if _is_back(name):

                    continue

                if not name:

                    console.print("[yellow]Name cannot be empty.[/yellow]")

                    continue



                use_latest = (

                    input("Use latest-source mode (forward newest message from channels/groups)... (y/N): ")

                    .strip()

                    .lower()

                    == "y"

                )



                latest_sources: list[str] = []

                links: list[str] = []

                back_out = False



                if use_latest:

                    console.print(

                        "Paste source channels/groups one per line (t.me/..., @username, or -100... ids). "

                        "Empty line to finish. Type 'b' to go back."

                    )

                    while True:

                        line = input().strip()

                        if not line:

                            break

                        if _is_back(line) and not latest_sources:

                            back_out = True

                            break

                        if _is_back(line):

                            console.print("[yellow]You already started adding sources. Finish with an empty line.[/yellow]")

                            continue

                        latest_sources.append(line)

                    if back_out:

                        continue

                    if not latest_sources:

                        console.print("[yellow]No sources added.[/yellow]")

                        continue

                else:

                    console.print("Paste message links one per line. Empty line to finish. Type 'b' to go back.")

                    while True:

                        line = input().strip()

                        if not line:

                            break

                        if _is_back(line) and not links:

                            back_out = True

                            break

                        if _is_back(line):

                            console.print("[yellow]You already started adding links. Finish with an empty line.[/yellow]")

                            continue

                        links.append(line)



                    if back_out:

                        continue



                    if not links:

                        console.print("[yellow]No message links added.[/yellow]")

                        continue



                _print_targets(targets)



                raw_sel = input("Select targets for this Ad (example 1,3,5-9 or all). 'b' to go back: ").strip()

                if _is_back(raw_sel):

                    continue

                idxs = parse_selection(raw_sel, max_index=len(targets))

                if not idxs:

                    console.print("[yellow]No targets selected.[/yellow]")

                    continue



                chosen = [targets[i - 1] for i in idxs]

                interval = _read_message_interval(

                    int(getattr(settings, "default_send_gap_min_sec", 60) or 60),

                    int(getattr(settings, "default_send_gap_max_sec", 120) or 120),

                )

                if interval is None:

                    continue

                send_min, send_max = interval



                days_mode = getattr(settings, "default_schedule_days", "all")

                windows = getattr(settings, "default_schedule_windows", None)

                windows_weekday = None

                windows_weekend = None

                sleep_start = getattr(settings, "default_sleep_start", "")

                sleep_end = getattr(settings, "default_sleep_end", "")

                sched_ans = input("Configure schedule windows & sleep hours... (y/N): ").strip().lower()

                if sched_ans == "y":

                    days_mode, windows, sleep_start, sleep_end, windows_weekday, windows_weekend = _prompt_schedule(

                        default_days=days_mode,

                        default_windows=windows,

                        default_sleep_start=sleep_start,

                        default_sleep_end=sleep_end,

                    )



                target_refs = [t.__dict__ for t in chosen]



                c = Campaign(

                    id=new_campaign_id(),

                    name=name,

                    message_links=links,

                    target_refs=target_refs,

                    send_gap_min_sec=send_min,

                    send_gap_max_sec=send_max,

                    batch_gap_min_sec=0,

                    batch_gap_max_sec=0,

                    schedule_days=days_mode,

                    schedule_windows=windows,

                    schedule_windows_weekday=windows_weekday,

                    schedule_windows_weekend=windows_weekend,

                    sleep_start=sleep_start,

                    sleep_end=sleep_end,

                    message_strategy="shuffle_bag",

                    target_strategy="shuffle_bag",

                    daily_cap=None,

                    per_target_cooldown_sec=None,

                    max_msgs_per_hour=None,

                    enabled=True,

                    adaptive_backoff_enabled=True,

                    warmup_enabled=False,

                    warmup_minutes=None,

                    warmup_start_multiplier=2.0,

                    warmup_end_multiplier=1.0,

                    bot_alert_mode=None,

                    bot_alert_every_n=None,

                    use_latest_source=use_latest,

                    latest_sources=latest_sources,

                    latest_source_strategy="round_robin",

                )

                save_campaign(c)



                console.print(f"[green]Saved Ad:[/green] {c.name} (id={c.id})")



            elif choice == "10":

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet. Use option 9.[/yellow]")

                    continue

                table = Table(title="Ads", show_header=True, header_style="bold magenta")

                table.add_column("#", justify="right", style="dim")

                table.add_column("Name", style="cyan")

                table.add_column("Msgs", justify="right")

                table.add_column("Targets", justify="right")

                table.add_column("Sources", justify="right")

                table.add_column("Mode", justify="center")

                table.add_column("ID", style="dim")

                for i, c in enumerate(ads, start=1):

                    use_latest = bool(getattr(c, "use_latest_source", False))

                    sources = len(getattr(c, "latest_sources", []) or []) if use_latest else 0

                    mode = "latest" if use_latest else "links"

                    table.add_row(

                        str(i),

                        c.name,

                        str(len(c.message_links)),

                        str(len(c.target_refs)),

                        str(sources) if use_latest else "-",

                        mode,

                        c.id,

                    )

                console.print(table)



            elif choice == "11":

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet. Use option 9.[/yellow]")

                    continue

                c = _pick_ad(ads, prompt="Edit which ad number: ")

                if c is None:

                    continue

                _edit_ad_flow(c)



            elif choice == "12":

                if tg is None:

                    console.print("[red]Please login first (option 2).[/red]")

                    continue

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet. Use option 9.[/yellow]")

                    continue

                c = _pick_ad(ads)

                if c is None:

                    continue

                ok, allow_risky = _preflight_ad(c, dry=True)

                if not ok:

                    continue

                ok, msg = await _start_ad_background(

                    c,

                    dry=True,

                    allow_risky=allow_risky,

                    suppress_updates_fn=lambda: suppress_updates,

                )

                if ok:

                    console.print("[green]Ad running in background.[/green]")

                else:

                    console.print(f"[yellow]Start failed:[/yellow] {msg}")

                await _input_async("Press Enter to return to menu (updates continue)...")



            elif choice == "13":

                if tg is None:

                    console.print("[red]Please login first (option 2).[/red]")

                    continue

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet. Use option 9.[/yellow]")

                    continue

                c = _pick_ad(ads)

                if c is None:

                    continue

                ok, allow_risky = _preflight_ad(c, dry=False)

                if not ok:

                    continue

                ok, msg = await _start_ad_background(

                    c,

                    dry=False,

                    allow_risky=allow_risky,

                    suppress_updates_fn=lambda: suppress_updates,

                )

                if ok:

                    console.print("[green]Ad running in background.[/green]")

                else:

                    console.print(f"[yellow]Start failed:[/yellow] {msg}")

                await _input_async("Press Enter to return to menu (updates continue)...")



            elif choice == "14":

                _show_ad_status()



            elif choice == "15":

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet.[/yellow]")

                    continue

                c = _pick_ad(ads, prompt="Pause which ad number: ")

                if c is None:

                    continue

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                ok = pause_state(state_path, c.id)

                console.print("[green]Paused.[/green]" if ok else "[yellow]No state found yet for this ad.[/yellow]")



            elif choice == "16":

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet.[/yellow]")

                    continue

                c = _pick_ad(ads, prompt="Resume which ad number: ")

                if c is None:

                    continue

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                ok = resume_state(state_path, c.id)

                if ok and not getattr(c, "enabled", True):
                    c.enabled = True
                    replace_campaign(c)
                    console.print("[green]Resumed and enabled.[/green]")
                else:
                    console.print("[green]Resumed.[/green]" if ok else "[yellow]No state found yet for this ad.[/yellow]")



            elif choice == "17":

                ads = list_campaigns()

                if not ads:

                    console.print("[yellow]No ads yet.[/yellow]")

                    continue

                c = _pick_ad(ads, prompt="Stop which ad number: ")

                if c is None:

                    continue

                state_path = PROFILES_DIR / f"state_{c.id}.json"

                stopped = stop_state(state_path, c.id)

                console.print(
                    "[green]Stopped.[/green] [dim](ad remains enabled)[/dim]"
                    if stopped
                    else "[yellow]No state found yet for this ad.[/yellow]"
                )



            elif choice == "27":

                _export_all()



            elif choice == "26":

                _import_all()



            elif choice == "28":

                sub = _delete_destinations_menu()

                if sub == "1":

                    ok = delete_targets()

                    console.print(

                        "[green]Deleted destination targets.[/green]" if ok else "[yellow]Targets not found or could not delete.[/yellow]"

                    )

                elif sub == "2":

                    ok = delete_destinations_cache()

                    console.print(

                        "[green]Deleted destinations cache.[/green]" if ok else "[yellow]Cache not found or could not delete.[/yellow]"

                    )

                elif sub == "3":

                    ok = delete_config()

                    console.print("[green]Deleted config.[/green]" if ok else "[yellow]Config not found or could not delete.[/yellow]")

                else:

                    continue



            elif choice == "28c":

                ok = delete_config()

                console.print("[green]Deleted config.[/green]" if ok else "[yellow]Config not found or could not delete.[/yellow]")



            elif choice == "28b":

                ok = delete_destinations_cache()

                console.print(

                    "[green]Deleted destinations cache.[/green]" if ok else "[yellow]Cache not found or could not delete.[/yellow]"

                )



            elif choice == "28a":

                ok = delete_targets()

                console.print("[green]Deleted destination targets.[/green]" if ok else "[yellow]Targets not found or could not delete.[/yellow]")



            elif choice == "29":

                ok = delete_campaigns()

                console.print("[green]Deleted Ads.[/green]" if ok else "[yellow]Ads not found or could not delete.[/yellow]")



            elif choice == "30":

                deleted = delete_sessions()

                tg = None

                console.print(f"[green]Deleted session files:[/green] {deleted}. You must login again.")



            elif choice == "31":

                res = nuke_all()

                tg = None

                console.print("[green]Nuke completed.[/green]")

                console.print(str(res))



            elif choice == "32":

                settings = _edit_advanced_settings(settings)



            elif choice == "8":

                targets = load_targets()

                if not targets:

                    console.print("[yellow]No destination targets saved yet.[/yellow]")

                    continue



                while True:

                    _print_targets(targets)

                    sub = _manage_targets_menu()



                    if sub == "1":

                        raw = input("Delete which target numbers... (example 1,3,5-8): ").strip()

                        idxs = parse_selection(raw, max_index=len(targets))

                        if not idxs:

                            console.print("[yellow]Nothing selected.[/yellow]")

                            continue



                        before = len(targets)

                        targets = remove_targets(targets, idxs)

                        save_targets(targets)

                        console.print(f"[green]Deleted {before - len(targets)} targets.[/green]")



                    elif sub == "2":

                        cached = load_json(DESTINATIONS_CACHE, default=[])

                        if not cached:

                            console.print("[yellow]No cached destinations. Use option 4 first.[/yellow]")

                            continue



                        dests = [Destination(**d) for d in cached]

                        groups = [d for d in dests if d.kind == "group"]

                        if not groups:

                            console.print("[yellow]No groups found in cached destinations.[/yellow]")

                            continue



                        stars_map = _load_stars_map()



                        console.print("[bold]Groups[/bold]")

                        for i, g in enumerate(groups, start=1):

                            stars_cost = stars_map.get(g.id, None)

                            stars_tag = f"stars {stars_cost}" if isinstance(stars_cost, int) and stars_cost > 0 else ""

                            forum_flag = "" if getattr(g, "is_forum", False) else ""

                            console.print(f"{i}) {g.title} {forum_flag}  {stars_tag}".rstrip())



                        pick = input("Pick a group number to delete ALL its saved targets: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid input.[/yellow]")

                            continue

                        gi = int(pick)

                        if gi < 1 or gi > len(groups):

                            console.print("[yellow]Out of range.[/yellow]")

                            continue



                        group = groups[gi - 1]

                        before = len(targets)

                        targets = clear_targets_for_group(targets, group.id)

                        save_targets(targets)

                        console.print(f"[green]Deleted {before - len(targets)} targets for group:[/green] {group.title}")



                    elif sub == "3":

                        raw = input("Set extra delay for which target numbers... (example 1,3,5-8): ").strip()

                        idxs = parse_selection(raw, max_index=len(targets))

                        if not idxs:

                            console.print("[yellow]Nothing selected.[/yellow]")

                            continue

                        raw_delay = input("Extra delay seconds (blank to clear): ").strip()

                        if raw_delay == "":

                            delay_val = None

                        elif raw_delay.isdigit():

                            delay_val = int(raw_delay)

                        else:

                            console.print("[yellow]Invalid delay.[/yellow]")

                            continue

                        for i in idxs:

                            t = targets[i - 1]

                            t.extra_delay_sec = delay_val

                        save_targets(targets)

                        console.print("[green]Updated per-target delay.[/green]")



                    elif sub == "4":

                        break

                    else:

                        console.print("[yellow]Invalid choice.[/yellow]")



            elif choice == "0":

                console.print("Exiting...")

                if running_tasks:

                    for cid in list(running_tasks.keys()):

                        try:

                            state_path = PROFILES_DIR / f"state_{cid}.json"

                            stop_state(state_path, cid)

                        except Exception:

                            pass

                        info = running_tasks.get(cid) or {}

                        task = info.get("task")

                        try:

                            if task:

                                task.cancel()

                        except Exception:

                            pass

                    running_tasks.clear()

                break



            # V2.0 FEATURES - Analytics & History

            elif choice == "18":

                from app.menu_handlers import view_message_history
                await view_message_history()

                input("\nPress Enter to continue...")

                skip_pause_once = True



            elif choice == "19":

                from app.menu_handlers import view_ad_stats
                await view_ad_stats()

                input("\nPress Enter to continue...")

                skip_pause_once = True



            elif choice == "20":

                from app.menu_handlers import view_group_matrix
                await view_group_matrix()

                input("\nPress Enter to continue...")

                skip_pause_once = True



            elif choice == "21":

                from app.menu_handlers import export_history_csv
                await export_history_csv()

                input("\nPress Enter to continue...")

                skip_pause_once = True



            elif choice == "22":

                from app.menu_handlers import view_total_messages
                await view_total_messages()

                input("\nPress Enter to continue...")

                skip_pause_once = True



            elif choice == "23":

                token = input("Enter Telegram bot token (or 'b' to go back): ").strip()

                if _is_back(token):

                    continue

                if not token:

                    console.print("[yellow]Token cannot be empty.[/yellow]")

                    continue

                chat_id = input("Enter chat ID to receive alerts: ").strip()

                if _is_back(chat_id):

                    continue

                if not chat_id:

                    console.print("[yellow]Chat ID cannot be empty.[/yellow]")

                    continue



                save_bot_config(token=token, chat_id=chat_id, enabled=True, auto_start_on_login=True)

                console.print("[green]Bot configuration saved.[/green]")



                ans = input("Start bot now... (y/N): ").strip().lower()

                if ans == "y":

                    if bot_mgr is not None:

                        try:

                            bot_mgr.stop_background()

                        except Exception:

                            pass

                        _release_bot_lock()

                    bot_mgr = TelegramBotManager(token, chat_id, control=bot_control)

                    try:

                        if not _ensure_bot_lock():

                            console.print("[yellow]Bot already running in another instance. Attempting takeover...[/yellow]")

                            ok = await _force_bot_takeover(token, chat_id)

                            console.print("[green]Bot takeover complete.[/green]" if ok else "[yellow]Bot takeover failed.[/yellow]")

                            continue

                        bot_mgr.start_background()

                        console.print("[green]Bot started.[/green]")

                    except Exception as e:

                        console.print(f"[red]Failed to start bot:[/red] {e}")

                        _release_bot_lock()

                        bot_mgr = None



            elif choice == "24":

                cfg = load_bot_config()

                if not cfg:

                    console.print("[yellow]Bot not configured yet.[/yellow]")

                    continue

                enabled = bool(cfg.get("enabled", False))

                token_tail = str(cfg.get("token", ""))[-6:]

                alert_mode = cfg.get("alert_mode", "errors")

                alert_every = int(cfg.get("alert_every_n", 10) or 10)

                auto_start_on_login = bool(cfg.get("auto_start_on_login", True))

                console.print("[bold]Bot settings[/bold]")

                console.print(f"Enabled: {'yes' if enabled else 'no'}")

                console.print(f"Token: ***{token_tail}" if token_tail else "Token: (not set)")

                console.print(f"Chat ID: {cfg.get('chat_id', '-')}")

                console.print(f"Alert mode: {alert_mode} (every_n={alert_every})")

                console.print(f"Auto-start on login: {'yes' if auto_start_on_login else 'no'}")

                console.print("1) Toggle enabled/disabled")

                console.print("2) Update token & chat ID")

                console.print("3) Start bot")

                console.print("4) Stop bot")

                console.print("5) Delete bot config")

                console.print("6) Alert settings")

                console.print("7) Toggle auto-start on login")

                console.print("8) Back")

                sub = input("Choice: ").strip()

                if sub == "1":

                    cfg["enabled"] = not enabled

                    save_bot_config(

                        token=cfg.get("token", ""),

                        chat_id=cfg.get("chat_id", ""),

                        enabled=cfg["enabled"],

                        alert_mode=cfg.get("alert_mode", "errors"),

                        alert_every_n=int(cfg.get("alert_every_n", 10) or 10),

                        auto_start_on_login=bool(cfg.get("auto_start_on_login", True)),

                    )

                    console.print("[green]Updated bot enabled flag.[/green]")

                    if not cfg["enabled"] and bot_mgr is not None:

                        try:

                            bot_mgr.stop_background()

                        except Exception:

                            pass

                        _release_bot_lock()

                        bot_mgr = None

                elif sub == "2":

                    token = input("New bot token (or 'b' to go back): ").strip()

                    if _is_back(token):

                        continue

                    chat_id = input("New chat ID: ").strip()

                    if _is_back(chat_id):

                        continue

                    if not token or not chat_id:

                        console.print("[yellow]Token and chat ID are required.[/yellow]")

                        continue

                    save_bot_config(

                        token=token,

                        chat_id=chat_id,

                        enabled=cfg.get("enabled", True),

                        alert_mode=cfg.get("alert_mode", "errors"),

                        alert_every_n=int(cfg.get("alert_every_n", 10) or 10),

                        auto_start_on_login=bool(cfg.get("auto_start_on_login", True)),

                    )

                    console.print("[green]Bot configuration updated.[/green]")

                    if bot_mgr is not None:

                        try:

                            bot_mgr.stop_background()

                        except Exception:

                            pass

                        _release_bot_lock()

                        bot_mgr = None

                elif sub == "3":

                    cfg = load_bot_config()

                    if not cfg or not cfg.get("token") or not cfg.get("chat_id"):

                        console.print("[yellow]Bot config incomplete.[/yellow]")

                        continue

                    if bot_mgr is not None and bot_mgr.app is not None:

                        console.print("[yellow]Bot is already running.[/yellow]")

                        continue

                    if bot_mgr is None:

                        bot_mgr = TelegramBotManager(cfg["token"], cfg["chat_id"], control=bot_control)

                    try:

                        if not _ensure_bot_lock():

                            console.print("[yellow]Bot already running in another instance.[/yellow]")

                            continue

                        bot_mgr.start_background()

                        console.print("[green]Bot started.[/green]")

                    except Exception as e:

                        console.print(f"[red]Failed to start bot:[/red] {e}")

                        _release_bot_lock()

                        bot_mgr = None

                elif sub == "4":

                    if bot_mgr is None:

                        console.print("[yellow]Bot is not running.[/yellow]")

                    else:

                        try:

                            bot_mgr.stop_background()

                            bot_mgr = None

                            _release_bot_lock()

                            console.print("[green]Bot stopped.[/green]")

                        except Exception as e:

                            console.print(f"[red]Failed to stop bot:[/red] {e}")

                elif sub == "5":

                    if bot_mgr is not None:

                        try:

                            bot_mgr.stop_background()

                        except Exception:

                            pass

                        bot_mgr = None

                        _release_bot_lock()

                    deleted = delete_bot_config()

                    console.print("[green]Bot config deleted.[/green]" if deleted else "[yellow]Bot config not found.[/yellow]")

                elif sub == "6":

                    mode = (cfg.get("alert_mode") or "errors").strip().lower()

                    every_n = int(cfg.get("alert_every_n", 10) or 10)

                    console.print("[bold]Alert mode[/bold]")

                    console.print("1) Every send")

                    console.print("2) Summary every N sends")

                    console.print("3) Errors + start/stop only")

                    pick = input(f"Choice [{mode}]: ").strip()

                    if pick == "1":

                        mode = "every"

                    elif pick == "2":

                        mode = "summary"

                    elif pick == "3":

                        mode = "errors"

                    if mode == "summary":

                        n_raw = input(f"Send summary every N messages [{every_n}]: ").strip()

                        if n_raw.isdigit() and int(n_raw) > 0:

                            every_n = int(n_raw)

                    save_bot_config(

                        token=cfg.get("token", ""),

                        chat_id=cfg.get("chat_id", ""),

                        enabled=cfg.get("enabled", True),

                        alert_mode=mode,

                        alert_every_n=every_n,

                        auto_start_on_login=bool(cfg.get("auto_start_on_login", True)),

                    )

                    console.print(f"[green]Alert settings saved.[/green] mode={mode}, every_n={every_n}")

                elif sub == "7":

                    cfg["auto_start_on_login"] = not bool(cfg.get("auto_start_on_login", True))

                    save_bot_config(

                        token=cfg.get("token", ""),

                        chat_id=cfg.get("chat_id", ""),

                        enabled=cfg.get("enabled", True),

                        alert_mode=cfg.get("alert_mode", "errors"),

                        alert_every_n=int(cfg.get("alert_every_n", 10) or 10),

                        auto_start_on_login=bool(cfg.get("auto_start_on_login", True)),

                    )

                    console.print(

                        f"[green]Auto-start on login:[/green] {'ON' if cfg['auto_start_on_login'] else 'OFF'}"

                    )

                else:

                    continue



            elif choice == "25":

                cfg = load_bot_config()

                if not cfg or not cfg.get("token") or not cfg.get("chat_id"):

                    console.print("[yellow]Bot not configured yet.[/yellow]")

                    continue

                token = cfg["token"]

                chat_id = cfg["chat_id"]

                try:

                    if bot_mgr is not None and bot_mgr.bot is not None:

                        await bot_mgr.send_message(" Telegram Forwarder bot test: message received.")
                    else:

                        from telegram import Bot

                        bot = Bot(token=token)

                        await bot.send_message(chat_id=chat_id, text=" Telegram Forwarder bot test: message received.")
                    console.print("[green]Test message sent.[/green]")

                except Exception as e:

                    console.print(f"[red]Failed to send test message:[/red] {e}")



            elif choice == "34":

                if not running_tasks:

                    console.print("[yellow]No running ads.[/yellow]")

                    continue

                console.print("[bold]Running ads[/bold]")

                for i, (cid, info) in enumerate(running_tasks.items(), start=1):
                    mode = "DRY" if info.get("dry") else "LIVE"
                    started_at = info.get("started_at")
                    started_txt = started_at.strftime("%H:%M:%S %d/%m/%Y") if isinstance(started_at, datetime) else "-"
                    console.print(f"{i}) {info.get('name')} | {mode} | Started: {started_txt}")


            elif choice == "35":

                if not running_tasks:

                    console.print("[yellow]No running ads.[/yellow]")

                    continue

                items = list(running_tasks.items())

                for i, (cid, info) in enumerate(items, start=1):

                    console.print(f"{i}) {info.get('name')} (id={cid})")

                pick = input("Stop which running ad number: ").strip()

                if not pick.isdigit():

                    console.print("[yellow]Invalid input.[/yellow]")

                    continue

                idx = int(pick)

                if idx < 1 or idx > len(items):

                    console.print("[yellow]Out of range.[/yellow]")

                    continue

                cid, info = items[idx - 1]

                state_path = PROFILES_DIR / f"state_{cid}.json"

                stop_state(state_path, cid)

                task = info.get("task")

                if task:

                    task.cancel()

                running_tasks.pop(cid, None)

                console.print("[green]Stop requested for running ad.[/green]")



            elif choice == "36":

                if not running_tasks:

                    console.print("[yellow]No running ads.[/yellow]")

                    continue

                suppress_updates = not suppress_updates

                console.print(

                    f"[green]Live updates:[/green] {'ON' if not suppress_updates else 'PAUSED'}"

                )



            elif choice == "37":

                if not running_tasks:

                    console.print("[yellow]No running ads.[/yellow]")

                    continue

                from rich.panel import Panel

                from rich.live import Live

                import msvcrt



                def _render_live() -> Panel:

                    lines: list[str] = ["Live updates are ON. Press Enter to return to menu."]

                    lines.append("")

                    lines.append("Running ads:")

                    for cid, info in running_tasks.items():

                        mode = "DRY" if info.get("dry") else "LIVE"

                        state_path = PROFILES_DIR / f"state_{cid}.json"

                        st = load_state(state_path, cid)

                        next_at = _safe_parse_next_at(getattr(st, "next_at", None)) if st else None

                        paused = bool(getattr(st, "paused", False)) if st else False

                        status = "PAUSED" if paused else "ACTIVE"

                        if next_at:

                            diff = int((next_at - datetime.now()).total_seconds())

                            if diff < 0:

                                next_txt = f"{next_at.strftime('%H:%M:%S %d/%m/%Y')} (due now)"
                            else:

                                next_txt = f"{next_at.strftime('%H:%M:%S %d/%m/%Y')} (in {_fmt_next(diff)})"
                        else:

                            next_txt = "-"

                        lines.append(
                            f"- {info.get('name')} | {mode} | {status} | Next: {next_txt}"
                        )
                    lines.append("")

                    lines.append("Recent events:")

                    # Show only the latest event per ad, ordered by soonest next send.
                    ranked: list[tuple[datetime, str]] = []
                    for cid, info in running_tasks.items():
                        state_path = PROFILES_DIR / f"state_{cid}.json"
                        st = load_state(state_path, cid)
                        next_at = _safe_parse_next_at(getattr(st, "next_at", None)) if st else None
                        sort_key = next_at if isinstance(next_at, datetime) else datetime.max
                        buf = info.get("events")
                        if buf:
                            ranked.append((sort_key, list(buf)[-1]))

                    ranked.sort(key=lambda x: x[0])
                    for _, line in ranked[:20]:
                        lines.append(line)

                    return Panel("\n".join(lines), title="[bold]Live Updates[/bold]", border_style="green")



                prev_suppress_updates = suppress_updates
                suppress_updates = True
                console.clear()

                with Live(_render_live(), refresh_per_second=6, console=console) as live:

                    while True:

                        live.update(_render_live())

                        await asyncio.sleep(0.2)

                        if msvcrt.kbhit():

                            ch = msvcrt.getch()

                            if ch in (b"\r", b"\n"):

                                break

                suppress_updates = prev_suppress_updates



            elif choice == "33":

                while True:

                    sub = _accounts_menu()

                    if sub == "1":

                        label = input("Account label (optional): ").strip()

                        api_id_raw = input("API ID: ").strip()

                        api_hash = input("API HASH: ").strip()

                        phone = input("Phone (with +): ").strip()

                        if not api_id_raw.isdigit() or not api_hash or not phone:

                            console.print("[yellow]API ID, API HASH, and phone are required.[/yellow]")

                            continue

                        acc_id = add_account(

                            label=label,

                            api_id=int(api_id_raw),

                            api_hash=api_hash,

                            phone=phone,

                        )

                        console.print(f"[green]Account added.[/green] id={acc_id}")

                        if input("Set as active... (y/N): ").strip().lower() == "y":

                            set_active_account(acc_id)

                            console.print("[green]Active account set.[/green]")

                        if input("Add proxy now... (y/N): ").strip().lower() == "y":

                            ptype, host, port, user, pw = _prompt_proxy_fields()

                            update_account_proxy(

                                account_id=acc_id,

                                proxy_type=ptype,

                                proxy_host=host,

                                proxy_port=port,

                                proxy_user=user,

                                proxy_pass=pw,

                            )

                            console.print("[green]Proxy updated.[/green]")

                    elif sub == "2":

                        _print_accounts_table()

                    elif sub == "3":

                        _print_accounts()

                        pick = input("Set active account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        if set_active_account(int(pick)):

                            console.print("[green]Active account set.[/green]")

                        else:

                            console.print("[yellow]Account not found.[/yellow]")

                    elif sub == "4":

                        _print_accounts_table()

                        pick = input("Edit account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        acc = get_account(int(pick))

                        if acc is None:

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        _edit_account_basic(acc)

                        console.print("[green]Account updated.[/green]")

                    elif sub == "5":

                        _print_accounts()

                        pick = input("Update proxy for account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        if get_account(int(pick)) is None:

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        ptype, host, port, user, pw = _prompt_proxy_fields()

                        update_account_proxy(

                            account_id=int(pick),

                            proxy_type=ptype,

                            proxy_host=host,

                            proxy_port=port,

                            proxy_user=user,

                            proxy_pass=pw,

                        )

                        console.print("[green]Proxy updated.[/green]")

                    elif sub == "6":

                        _print_accounts()

                        pick = input("Manage proxy pool for account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        acc = get_account(int(pick))

                        if acc is None:

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        while True:

                            console.print("\n[bold]Proxy pool[/bold]")

                            console.print("1) List proxies")

                            console.print("2) Add proxy")

                            console.print("3) Delete proxy")

                            console.print("4) Clear proxy pool")

                            console.print("5) Back")

                            psub = input("Choice: ").strip()

                            if psub == "1":

                                _print_proxy_pool(acc.id)

                            elif psub == "2":

                                label = input("Proxy label (optional): ").strip()

                                ptype, host, port, user, pw = _prompt_proxy_fields()

                                if not host or not port:

                                    console.print("[yellow]Proxy not added.[/yellow]")

                                    continue

                                add_account_proxy(

                                    account_id=acc.id,

                                    label=label,

                                    proxy_type=ptype,

                                    proxy_host=host,

                                    proxy_port=port,

                                    proxy_user=user,

                                    proxy_pass=pw,

                                )

                                console.print("[green]Proxy added to pool.[/green]")

                            elif psub == "3":

                                _print_proxy_pool(acc.id)

                                pid = input("Delete proxy id: ").strip()

                                if not pid.isdigit():

                                    console.print("[yellow]Invalid proxy id.[/yellow]")

                                    continue

                                ok = delete_account_proxy(int(pid))

                                console.print("[green]Proxy deleted.[/green]" if ok else "[yellow]Proxy not found.[/yellow]")

                            elif psub == "4":

                                ans = input("Clear ALL proxies for this account... (y/N): ").strip().lower()

                                if ans != "y":

                                    continue

                                count = clear_account_proxies(acc.id)

                                console.print(f"[green]Cleared {count} proxies.[/green]")

                            else:

                                break

                    elif sub == "7":

                        _print_accounts()

                        pick = input("Proxy rotation settings for account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        acc = get_account(int(pick))

                        if acc is None:

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        mode = (acc.proxy_rotation_mode or "round_robin").strip().lower()

                        rotate_on_login = acc.proxy_rotation_on_login

                        if rotate_on_login is None:

                            rotate_on_login = True

                        console.print("Rotation mode:")

                        console.print("1) Fixed (always same)")

                        console.print("2) Round-robin")

                        console.print("3) Random")

                        m = input(f"Choice [{mode}]: ").strip()

                        if m == "1":

                            mode = "fixed"

                        elif m == "3":

                            mode = "random"

                        else:

                            mode = "round_robin"

                        rol = input(f"Rotate on login (y/N) [{'y' if rotate_on_login else 'n'}]: ").strip().lower()

                        if rol == "":

                            pass

                        else:

                            rotate_on_login = rol == "y"

                        ok = update_proxy_rotation_settings(

                            account_id=acc.id,

                            mode=mode,

                            rotate_on_login=rotate_on_login,

                        )

                        console.print("[green]Rotation settings updated.[/green]" if ok else "[yellow]Update failed.[/yellow]")

                    elif sub == "8":

                        _print_accounts()

                        pick = input("Update rate/window for account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        acc = get_account(int(pick))

                        if acc is None:

                            console.print("[yellow]Account not found.[/yellow]")

                            continue

                        rate_raw = input(f"Rate multiplier [{acc.rate_multiplier or settings.account_rate_multiplier_default}]: ").strip()

                        if rate_raw:

                            try:

                                rate_multiplier = float(rate_raw)

                            except Exception:

                                console.print("[yellow]Invalid rate multiplier. Keeping current.[/yellow]")

                                rate_multiplier = acc.rate_multiplier

                        else:

                            rate_multiplier = acc.rate_multiplier



                        days_raw = input(f"Send days [all/weekday/weekend] [{acc.send_days or 'all'}]: ").strip().lower()

                        if days_raw not in ("", "all", "weekday", "weekend"):

                            console.print("[yellow]Invalid days. Keeping current.[/yellow]")

                            days_raw = acc.send_days

                        send_days = days_raw or acc.send_days



                        start_raw = input(f"Send window start HH:MM [{acc.send_window_start or '-'}] (blank keep, 'none' clear): ").strip()

                        end_raw = input(f"Send window end HH:MM [{acc.send_window_end or '-'}] (blank keep, 'none' clear): ").strip()

                        if start_raw.lower() == "none" or end_raw.lower() == "none":

                            send_start = None

                            send_end = None

                        else:

                            send_start = acc.send_window_start if start_raw == "" else start_raw

                            send_end = acc.send_window_end if end_raw == "" else end_raw



                        ok = update_account_advanced(

                            account_id=int(pick),

                            rate_multiplier=rate_multiplier,

                            send_window_start=send_start,

                            send_window_end=send_end,

                            send_days=send_days,

                        )

                        console.print("[green]Account updated.[/green]" if ok else "[yellow]Update failed.[/yellow]")

                    elif sub == "9":

                        _print_accounts()

                        pick = input("Delete account id: ").strip()

                        if not pick.isdigit():

                            console.print("[yellow]Invalid account id.[/yellow]")

                            continue

                        ans = input("Delete account permanently... (y/N): ").strip().lower()

                        if ans != "y":

                            continue

                        ok = delete_account(int(pick))

                        console.print("[green]Account deleted.[/green]" if ok else "[yellow]Account not found.[/yellow]")

                    else:

                        break



            else:

                console.print("[yellow]Invalid option.[/yellow]")



    finally:

        try:

            if bot_mgr is not None:

                try:

                    bot_mgr.send_message_background(

                        "🔌 Telegram Forwarder is going offline.\n🧯 Ads and bot are stopping now."
                    )

                except Exception:

                    pass

            if tg is not None:

                await tg.close()

            if bot_mgr is not None:

                bot_mgr.stop_background()

                _release_bot_lock()

            if running_tasks:

                for cid in list(running_tasks.keys()):

                    try:

                        state_path = PROFILES_DIR / f"state_{cid}.json"

                        stop_state(state_path, cid)

                    except Exception:

                        pass

                    info = running_tasks.get(cid) or {}

                    task = info.get("task")

                    try:

                        if task:

                            task.cancel()

                    except Exception:

                        pass

        finally:

            if app_lock is not None:

                app_lock.release()





def main() -> None:

    try:

        asyncio.run(run_app())

    except KeyboardInterrupt:

        console.print("[yellow]Exiting...[/yellow]")





if __name__ == "__main__":

    main()

LAST_RUN_PATH = DATA_DIR / "last_run.json"





def _save_last_run(*, ad_id: str, ad_name: str, mode: str) -> None:

    try:

        save_json(

            LAST_RUN_PATH,

            {

                "ad_id": ad_id,

                "ad_name": ad_name,

                "mode": mode,

                "ts": datetime.now().isoformat(),

            },

        )

    except Exception:

        pass





def _print_last_run() -> None:

    data = load_json(LAST_RUN_PATH, default=None)

    if not isinstance(data, dict):

        return

    ts = data.get("ts")

    name = data.get("ad_name", "...")

    mode = data.get("mode", "...")

    if not ts:

        return

    try:

        dt = datetime.fromisoformat(ts)

        ts_txt = dt.strftime("%H:%M:%S %d/%m/%Y")
    except Exception:

        ts_txt = str(ts)

    console.print(f"[dim]Last Ad run: {name} | {mode} | {ts_txt}[/dim]")
