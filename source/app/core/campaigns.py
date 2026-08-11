from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.utils.paths import PROFILES_DIR
from app.utils.storage import load_json, save_json, update_json

CAMPAIGNS_PATH = PROFILES_DIR / "campaigns.json"


@dataclass
class Campaign:
    id: str
    name: str

    # message links to forward (creatives)
    message_links: List[str]

    # stored targets snapshot so the Ad remains stable even if targets change later
    target_refs: List[dict]

    # scheduling
    send_gap_min_sec: int
    send_gap_max_sec: int
    batch_gap_min_sec: int
    batch_gap_max_sec: int

    # latest-source mode (forward latest message from sources)
    use_latest_source: bool = False
    latest_sources: List[str] = None
    latest_source_strategy: str = "round_robin"
    schedule_days: str = "all"  # "all" | "weekday" | "weekend"
    schedule_windows: Optional[List[Dict[str, str]]] = None
    schedule_windows_weekday: Optional[List[Dict[str, str]]] = None
    schedule_windows_weekend: Optional[List[Dict[str, str]]] = None
    sleep_start: str = ""
    sleep_end: str = ""

    # behavior
    message_strategy: str = "shuffle_bag"   # "shuffle_bag" | "round_robin"
    target_strategy: str = "shuffle_bag"    # "shuffle_bag" | "round_robin"

    # safety and quality of life
    daily_cap: Optional[int] = None
    per_target_cooldown_sec: Optional[int] = None
    max_msgs_per_hour: Optional[int] = None
    per_target_daily_cap: Optional[int] = None
    enabled: bool = True

    # Smart delay + warm-up
    adaptive_backoff_enabled: bool = True
    warmup_enabled: bool = False
    warmup_minutes: Optional[int] = None
    warmup_start_multiplier: float = 2.0
    warmup_end_multiplier: float = 1.0

    # bot alert overrides (optional)
    bot_alert_mode: Optional[str] = None
    bot_alert_every_n: Optional[int] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Campaign":
        d2 = dict(d)

        # Backward-compatible defaults for older saved Ads
        if "enabled" not in d2:
            d2["enabled"] = True
        if "message_strategy" not in d2:
            d2["message_strategy"] = "shuffle_bag"
        if "target_strategy" not in d2:
            d2["target_strategy"] = "shuffle_bag"
        if "daily_cap" not in d2:
            d2["daily_cap"] = None
        if "per_target_cooldown_sec" not in d2:
            d2["per_target_cooldown_sec"] = None
        if "max_msgs_per_hour" not in d2:
            d2["max_msgs_per_hour"] = None
        if "per_target_daily_cap" not in d2:
            d2["per_target_daily_cap"] = None
        if "schedule_days" not in d2:
            d2["schedule_days"] = "all"
        if "schedule_windows" not in d2:
            d2["schedule_windows"] = None
        if "schedule_windows_weekday" not in d2:
            d2["schedule_windows_weekday"] = None
        if "schedule_windows_weekend" not in d2:
            d2["schedule_windows_weekend"] = None
        if "sleep_start" not in d2:
            d2["sleep_start"] = ""
        if "sleep_end" not in d2:
            d2["sleep_end"] = ""
        if "bot_alert_mode" not in d2:
            d2["bot_alert_mode"] = None
        if "bot_alert_every_n" not in d2:
            d2["bot_alert_every_n"] = None
        d2.pop("allow_paid_targets", None)
        d2.pop("paid_require_confirm", None)
        d2.pop("paid_auto_pay", None)
        d2.pop("paid_max_stars_per_send", None)
        d2.pop("paid_max_stars_per_day", None)
        d2.pop("paid_max_paid_groups_per_day", None)
        if "adaptive_backoff_enabled" not in d2:
            d2["adaptive_backoff_enabled"] = True
        if "warmup_enabled" not in d2:
            d2["warmup_enabled"] = False
        if "warmup_minutes" not in d2:
            d2["warmup_minutes"] = None
        if "warmup_start_multiplier" not in d2:
            d2["warmup_start_multiplier"] = 2.0
        if "warmup_end_multiplier" not in d2:
            d2["warmup_end_multiplier"] = 1.0
        if "use_latest_source" not in d2:
            d2["use_latest_source"] = False
        if "latest_sources" not in d2 or d2["latest_sources"] is None:
            d2["latest_sources"] = []
        if "latest_source_strategy" not in d2:
            d2["latest_source_strategy"] = "round_robin"

        return cls(**d2)


def _load_raw() -> List[dict]:
    raw = load_json(CAMPAIGNS_PATH, default=[])
    return raw if isinstance(raw, list) else []


def _save_raw(items: List[dict]) -> None:
    save_json(CAMPAIGNS_PATH, items)


def list_campaigns() -> List[Campaign]:
    raw = _load_raw()
    out: List[Campaign] = []
    for c in raw:
        if isinstance(c, dict):
            out.append(Campaign.from_dict(c))
    return out


def save_campaign(campaign: Campaign) -> None:
    raw = _load_raw()
    raw.append(asdict(campaign))
    _save_raw(raw)


def replace_campaign(campaign: Campaign) -> None:
    def _replace(raw: Any) -> List[dict]:
        items = raw if isinstance(raw, list) else []
        new_raw: List[dict] = []
        replaced = False
        for item in items:
            if isinstance(item, dict) and item.get("id") == campaign.id:
                new_raw.append(asdict(campaign))
                replaced = True
            else:
                new_raw.append(item if isinstance(item, dict) else {})
        if not replaced:
            new_raw.append(asdict(campaign))
        return new_raw

    update_json(CAMPAIGNS_PATH, [], _replace)


def remove_campaign_target(campaign_id: str, group_id: int, topic_id: Optional[int]) -> bool:
    removed = False

    def _remove(raw: Any) -> List[dict]:
        nonlocal removed
        items = raw if isinstance(raw, list) else []
        updated: List[dict] = []
        for item in items:
            if not isinstance(item, dict) or item.get("id") != campaign_id:
                updated.append(item if isinstance(item, dict) else {})
                continue
            campaign = dict(item)
            next_targets: List[dict] = []
            for target in list(campaign.get("target_refs") or []):
                try:
                    target_group = int(target.get("group_id"))
                    raw_topic = target.get("topic_id")
                    target_topic = int(raw_topic) if raw_topic is not None else None
                except (AttributeError, TypeError, ValueError):
                    next_targets.append(target)
                    continue
                if target_group == int(group_id) and target_topic == topic_id:
                    removed = True
                    continue
                next_targets.append(target)
            campaign["target_refs"] = next_targets
            updated.append(campaign)
        return updated

    update_json(CAMPAIGNS_PATH, [], _remove)
    return removed


def get_campaign(campaign_id: str) -> Optional[Campaign]:
    for c in list_campaigns():
        if c.id == campaign_id:
            return c
    return None


def delete_campaign(campaign_id: str) -> bool:
    raw = _load_raw()
    new_raw: List[dict] = []
    removed = False
    for c in raw:
        if isinstance(c, dict) and c.get("id") == campaign_id:
            removed = True
            continue
        new_raw.append(c if isinstance(c, dict) else {})
    if removed:
        _save_raw(new_raw)
    return removed


def new_campaign_id() -> str:
    return uuid4().hex[:10]
