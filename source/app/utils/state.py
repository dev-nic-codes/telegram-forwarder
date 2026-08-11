from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.utils.storage import load_json, save_json


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


@dataclass
class CampaignState:
    campaign_id: str

    # shuffle bags and round-robin indices
    msg_bag: List[int]
    tgt_bag: List[int]
    msg_rr_idx: int = 0
    tgt_rr_idx: int = 0

    # batch control
    sent_in_current_batch: int = 0

    # time control
    next_at: Optional[datetime] = None
    start_at: Optional[datetime] = None

    # counters
    sent_total: int = 0
    error_streak: int = 0
    last_error_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    hour_window_start: Optional[datetime] = None
    hour_sent_count: int = 0
    day_date: Optional[str] = None
    day_sent_count: int = 0
    per_target_day_counts: Dict[str, int] = None
    per_target_day_date: Optional[str] = None

    # latest-source mode
    src_rr_idx: int = 0
    current_source_id: Optional[str] = None
    current_source_msg_id: Optional[int] = None
    last_source_message_ids: Dict[str, int] = None

    # target health
    target_fail_counts: Dict[str, int] = None
    target_disabled: Dict[str, Dict[str, Any]] = None

    # pause control
    paused: bool = False
    paused_at: Optional[datetime] = None
    paused_reason: Optional[str] = None

    # stop control
    stopped: bool = False
    stopped_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["next_at"] = _dt_to_str(self.next_at)
        d["start_at"] = _dt_to_str(self.start_at)
        d["paused_at"] = _dt_to_str(self.paused_at)
        d["stopped_at"] = _dt_to_str(self.stopped_at)
        d["last_error_at"] = _dt_to_str(self.last_error_at)
        d["last_success_at"] = _dt_to_str(self.last_success_at)
        d["hour_window_start"] = _dt_to_str(self.hour_window_start)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CampaignState":
        d2 = dict(d)

        d2["next_at"] = _str_to_dt(d2.get("next_at"))
        d2["start_at"] = _str_to_dt(d2.get("start_at"))
        d2["paused_at"] = _str_to_dt(d2.get("paused_at"))
        d2["stopped_at"] = _str_to_dt(d2.get("stopped_at"))
        d2["last_error_at"] = _str_to_dt(d2.get("last_error_at"))
        d2["last_success_at"] = _str_to_dt(d2.get("last_success_at"))
        d2["hour_window_start"] = _str_to_dt(d2.get("hour_window_start"))

        # backward compatibility with old state files
        if "paused" not in d2:
            d2["paused"] = False
        if "start_at" not in d2:
            d2["start_at"] = None
        if "paused_at" not in d2:
            d2["paused_at"] = None
        if "stopped" not in d2:
            d2["stopped"] = False
        if "stopped_at" not in d2:
            d2["stopped_at"] = None
        if "error_streak" not in d2:
            d2["error_streak"] = 0
        if "last_error_at" not in d2:
            d2["last_error_at"] = None
        if "last_success_at" not in d2:
            d2["last_success_at"] = None
        if "hour_window_start" not in d2:
            d2["hour_window_start"] = None
        if "hour_sent_count" not in d2:
            d2["hour_sent_count"] = 0
        if "day_date" not in d2:
            d2["day_date"] = None
        if "day_sent_count" not in d2:
            d2["day_sent_count"] = 0
        if "per_target_day_counts" not in d2 or d2["per_target_day_counts"] is None:
            d2["per_target_day_counts"] = {}
        if "per_target_day_date" not in d2:
            d2["per_target_day_date"] = None
        if "paused_reason" not in d2:
            d2["paused_reason"] = None
        if "src_rr_idx" not in d2:
            d2["src_rr_idx"] = 0
        if "current_source_id" not in d2:
            d2["current_source_id"] = None
        if "current_source_msg_id" not in d2:
            d2["current_source_msg_id"] = None
        if "last_source_message_ids" not in d2 or d2["last_source_message_ids"] is None:
            d2["last_source_message_ids"] = {}
        if "target_fail_counts" not in d2 or d2["target_fail_counts"] is None:
            d2["target_fail_counts"] = {}
        if "target_disabled" not in d2 or d2["target_disabled"] is None:
            d2["target_disabled"] = {}
        d2.pop("pending_payments", None)
        d2.pop("paid_day_date", None)
        d2.pop("paid_day_stars", None)
        d2.pop("paid_day_groups", None)

        return cls(**d2)


def load_state(path: Path, campaign_id: str) -> Optional[CampaignState]:
    raw = load_json(path, default=None)
    if not raw or not isinstance(raw, dict):
        return None
    if raw.get("campaign_id") != campaign_id:
        return None
    try:
        return CampaignState.from_dict(raw)
    except Exception:
        return None


def save_state(path: Path, state: CampaignState) -> None:
    save_json(path, state.to_dict())


# helpers for pause / resume control

def pause_state(path: Path, campaign_id: str) -> bool:
    state = load_state(path, campaign_id)
    if not state:
        return False
    if not state.paused:
        state.paused = True
        state.paused_at = datetime.now()
        state.paused_reason = "manual"
        save_state(path, state)
    return True


def resume_state(path: Path, campaign_id: str) -> bool:
    state = load_state(path, campaign_id)
    if not state:
        return False
    state.paused = False
    state.paused_at = None
    state.paused_reason = None
    state.stopped = False
    state.stopped_at = None
    # Manual resume is an explicit operator override: clear temporary health blocks.
    state.error_streak = 0
    state.target_fail_counts = {}
    state.target_disabled = {}
    # resume immediately
    state.next_at = datetime.now()
    save_state(path, state)
    return True


def stop_state(path: Path, campaign_id: str) -> bool:
    state = load_state(path, campaign_id)
    if not state:
        return False
    state.paused = False
    state.paused_at = None
    state.paused_reason = None
    state.stopped = True
    state.stopped_at = datetime.now()
    save_state(path, state)
    return True
