from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.utils.paths import DATA_DIR
from app.utils.storage import load_json, save_json

SETTINGS_PATH = DATA_DIR / "settings.json"


@dataclass
class AdvancedSettings:
    # Scheduling & pacing
    global_quiet_hours_enabled: bool = False
    global_quiet_start: str = "00:00"
    global_quiet_end: str = "00:00"
    global_jitter_min_sec: int = 0
    global_jitter_max_sec: int = 0
    global_min_send_gap_sec: int = 1

    # Safety & guardrails
    max_next_at_drift_sec: Optional[int] = None
    global_max_msgs_per_hour: Optional[int] = None
    kill_switch_error_streak: Optional[int] = None

    # Reliability & error control
    max_error_streak_pause: Optional[int] = 5
    auto_resume_minutes: Optional[int] = None
    continue_on_target_error: bool = True
    retry_transient_count: int = 0
    retry_transient_base_delay_sec: float = 2.0
    cooldown_timeout_sec: int = 120
    cooldown_topic_closed_sec: int = 900
    cooldown_not_in_forum_sec: int = 600
    cooldown_generic_error_sec: int = 60
    cooldown_flood_min_sec: int = 5
    cooldown_flood_max_sec: int = 3600
    latest_source_poll_sec: int = 60

    # Group health
    auto_disable_target_error_count: int = 3
    auto_disable_target_minutes: Optional[int] = 1440
    auto_disable_on_no_permission: bool = True
    auto_disable_on_dead_group: bool = True

    # Smart delay + warm-up tuning
    backoff_step: float = 0.5
    backoff_max_multiplier: float = 4.0

    # Account & session
    account_rate_multiplier_default: float = 1.0
    force_reconnect_minutes: Optional[int] = None

    # Bot & UI
    bot_quiet_hours_enabled: bool = False
    bot_quiet_start: str = "00:00"
    bot_quiet_end: str = "00:00"
    live_update_every_sec: int = 10

    # Logging & history
    log_retention_days: Optional[int] = 30
    history_retention_days: Optional[int] = None
    auto_export_hours: Optional[int] = None
    last_auto_export_at: Optional[str] = None

    # Defaults for new ads
    default_send_gap_min_sec: int = 60
    default_send_gap_max_sec: int = 120
    # Retained in persisted settings for backward compatibility; cycle gaps are disabled.
    default_batch_gap_min_sec: int = 0
    default_batch_gap_max_sec: int = 0
    default_schedule_days: str = "all"
    default_schedule_windows: Optional[List[Dict[str, str]]] = None
    default_sleep_start: str = ""
    default_sleep_end: str = ""


def _coerce_int(v: Any, default: Optional[int]) -> Optional[int]:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def load_settings() -> AdvancedSettings:
    data = load_json(SETTINGS_PATH, default=None)
    if not data or not isinstance(data, dict):
        return AdvancedSettings()

    s = AdvancedSettings()
    for k, v in data.items():
        if hasattr(s, k):
            setattr(s, k, v)

    # coerce types
    s.global_jitter_min_sec = _coerce_int(s.global_jitter_min_sec, 0) or 0
    s.global_jitter_max_sec = _coerce_int(s.global_jitter_max_sec, 0) or 0
    s.global_min_send_gap_sec = _coerce_int(s.global_min_send_gap_sec, 1) or 1
    s.max_next_at_drift_sec = _coerce_int(s.max_next_at_drift_sec, None)
    s.global_max_msgs_per_hour = _coerce_int(s.global_max_msgs_per_hour, None)
    s.kill_switch_error_streak = _coerce_int(s.kill_switch_error_streak, None)
    s.max_error_streak_pause = _coerce_int(s.max_error_streak_pause, 5)
    s.auto_resume_minutes = _coerce_int(s.auto_resume_minutes, None)
    s.retry_transient_count = _coerce_int(s.retry_transient_count, 0) or 0
    s.retry_transient_base_delay_sec = _coerce_float(s.retry_transient_base_delay_sec, 2.0)
    s.cooldown_timeout_sec = _coerce_int(s.cooldown_timeout_sec, 120) or 120
    s.cooldown_topic_closed_sec = _coerce_int(s.cooldown_topic_closed_sec, 900) or 900
    s.cooldown_not_in_forum_sec = _coerce_int(s.cooldown_not_in_forum_sec, 600) or 600
    s.cooldown_generic_error_sec = _coerce_int(s.cooldown_generic_error_sec, 60) or 60
    s.cooldown_flood_min_sec = _coerce_int(s.cooldown_flood_min_sec, 5) or 5
    s.cooldown_flood_max_sec = _coerce_int(s.cooldown_flood_max_sec, 3600) or 3600
    s.latest_source_poll_sec = _coerce_int(s.latest_source_poll_sec, 60) or 60
    s.auto_disable_target_error_count = _coerce_int(s.auto_disable_target_error_count, 3) or 3
    s.auto_disable_target_minutes = _coerce_int(s.auto_disable_target_minutes, 1440)
    s.backoff_step = _coerce_float(s.backoff_step, 0.5)
    s.backoff_max_multiplier = _coerce_float(s.backoff_max_multiplier, 4.0)
    s.account_rate_multiplier_default = _coerce_float(s.account_rate_multiplier_default, 1.0)
    s.force_reconnect_minutes = _coerce_int(s.force_reconnect_minutes, None)
    s.live_update_every_sec = _coerce_int(s.live_update_every_sec, 10) or 10
    s.log_retention_days = _coerce_int(s.log_retention_days, 30)
    s.history_retention_days = _coerce_int(s.history_retention_days, None)
    s.auto_export_hours = _coerce_int(s.auto_export_hours, None)
    return s


def save_settings(settings: AdvancedSettings) -> None:
    save_json(SETTINGS_PATH, asdict(settings))


def update_last_export(settings: AdvancedSettings, when: Optional[datetime] = None) -> AdvancedSettings:
    ts = (when or datetime.now()).isoformat()
    settings.last_auto_export_at = ts
    save_settings(settings)
    return settings
