from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import ForwardMessagesRequest
from telethon.tl.types import PeerChannel, PeerChat

from app.core.campaigns import Campaign, remove_campaign_target
from app.core.send_coordinator import DestinationSendCoordinator
from app.core.sources import resolve_source_entity
from app.utils.paths import DESTINATIONS_CACHE, LOGS_DIR
from app.utils.state import CampaignState, load_state, save_state
from app.utils.storage import load_json
from app.utils.schedule import is_allowed_now, next_allowed_time
from app.utils.settings import AdvancedSettings, load_settings


from app.analytics.history_tracker import get_history
EventCb = Callable[[Dict[str, Any]], None]


class _SourceUnavailableError(Exception):
    def __init__(self, source: str, original: Exception) -> None:
        super().__init__(str(original))
        self.source = source
        self.original = original


def _rand_between(rng: random.Random, a: int, b: int) -> int:
    lo = min(a, b)
    hi = max(a, b)
    return int(rng.randint(lo, hi))


def _label_target(t: dict) -> str:
    gt = str(t.get("group_title", "Unknown"))
    tt = t.get("topic_title", None)
    if tt:
        return f"{gt} -> {tt}"
    return f"{gt} (no topic)"


def _target_peer_ref(target: dict):
    group_id = int(target["group_id"])
    peer_type = str(target.get("peer_type") or "channel").strip().casefold()
    if peer_type == "chat":
        return PeerChat(group_id)
    return PeerChannel(group_id)


def _load_stars_map() -> Dict[int, Optional[int]]:
    stars: Dict[int, Optional[int]] = {}
    raw = load_json(DESTINATIONS_CACHE, default=[])
    for d in raw:
        try:
            gid = int(d.get("id"))
        except Exception:
            continue
        v = d.get("paid_message_stars", None)
        if v is None:
            stars[gid] = None
        else:
            try:
                stars[gid] = int(v)
            except Exception:
                stars[gid] = None
    return stars


def _log_line(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H:%M:%S %d/%m/%Y")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {line}\n")


def _emit(on_event: Optional[EventCb], payload: Dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(payload)
    except Exception:
        # UI must never break the runner
        return


async def _retry_if_db_locked(fn, *, tries: int = 6, base_delay: float = 0.4):
    """
    Telethon writes entities to the session SQLite during many API calls.
    If another process is using the same session file, we can get 'database is locked'.
    Retry a few times with backoff, then re-raise.
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
    if last_err is not None:
        raise last_err


def _parse_message_link(link: str) -> Tuple[Any, int]:
    link = (link or "").strip()
    if not link:
        raise ValueError("Message link is empty")

    if "/c/" in link:
        tail = link.split("/c/", 1)[1]
        parts = [p.strip() for p in tail.split("/") if p.strip()]
        if len(parts) < 2 or any(not part.isdigit() for part in parts):
            raise ValueError(f"Invalid Telegram /c/ message link: {link}")
        peer = int(f"-100{parts[0]}")
        return peer, int(parts[-1])

    if "t.me/" not in link:
        raise ValueError(f"Invalid Telegram message link: {link}")

    tail = link.split("t.me/", 1)[1]
    parts = [p.strip() for p in tail.split("/") if p.strip()]
    if len(parts) < 2 or any(not part.isdigit() for part in parts[1:]):
        raise ValueError(f"Invalid Telegram message link: {link}")

    username = parts[0].lstrip("@")
    if not username:
        raise ValueError(f"Invalid Telegram message link: {link}")
    return username, int(parts[-1])


def _normalize_source_ref(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "t.me/" in s:
        s = s.split("t.me/")[1]
    s = s.lstrip("/")
    if s.startswith("c/"):
        parts = s.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return f"-100{parts[1]}"
    if "/" in s:
        s = s.split("/")[0]
    if s.startswith("@"):
        s = s[1:]
    return s.strip()


def _coerce_source_peer(ref: str):
    if ref.lstrip("-").isdigit():
        return int(ref)
    return ref


def _source_key(peer_ref) -> str:
    if isinstance(peer_ref, str):
        return peer_ref.lower()
    return str(peer_ref)


def _sanitize_index_bag(values, size: int) -> list[int]:
    if size <= 0 or not isinstance(values, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        try:
            idx = int(raw)
        except Exception:
            continue
        if idx < 0 or idx >= size or idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
    return out


def _sanitize_rr_index(value, size: int) -> int:
    if size <= 0:
        return 0
    try:
        idx = int(value)
    except Exception:
        return 0
    if idx < 0 or idx >= size:
        return 0
    return idx


def _sanitize_runtime_state(
    state: CampaignState,
    campaign: Campaign,
    *,
    latest_sources: list[str],
    use_latest: bool,
) -> bool:
    changed = False

    msg_total = len(campaign.message_links or [])
    tgt_total = len(campaign.target_refs or [])

    msg_bag = _sanitize_index_bag(getattr(state, "msg_bag", []), msg_total)
    if msg_bag != list(getattr(state, "msg_bag", []) or []):
        state.msg_bag = msg_bag
        changed = True

    tgt_bag = _sanitize_index_bag(getattr(state, "tgt_bag", []), tgt_total)
    if tgt_bag != list(getattr(state, "tgt_bag", []) or []):
        state.tgt_bag = tgt_bag
        changed = True

    msg_rr_idx = _sanitize_rr_index(getattr(state, "msg_rr_idx", 0), msg_total)
    if msg_rr_idx != getattr(state, "msg_rr_idx", 0):
        state.msg_rr_idx = msg_rr_idx
        changed = True

    tgt_rr_idx = _sanitize_rr_index(getattr(state, "tgt_rr_idx", 0), tgt_total)
    if tgt_rr_idx != getattr(state, "tgt_rr_idx", 0):
        state.tgt_rr_idx = tgt_rr_idx
        changed = True

    normalized_source_keys: set[str] = set()
    normalized_source_count = 0
    for raw in latest_sources:
        norm = _normalize_source_ref(raw)
        if not norm:
            continue
        normalized_source_count += 1
        normalized_source_keys.add(_source_key(_coerce_source_peer(norm)))

    src_total = normalized_source_count
    if use_latest and src_total > 0:
        try:
            src_rr_idx = int(getattr(state, "src_rr_idx", 0) or 0) % src_total
        except Exception:
            src_rr_idx = 0
    else:
        src_rr_idx = 0
    if src_rr_idx != getattr(state, "src_rr_idx", 0):
        state.src_rr_idx = src_rr_idx
        changed = True

    current_source_id = getattr(state, "current_source_id", None)
    if not use_latest or (current_source_id is not None and current_source_id not in normalized_source_keys):
        if current_source_id is not None:
            state.current_source_id = None
            changed = True
        if getattr(state, "current_source_msg_id", None) is not None:
            state.current_source_msg_id = None
            changed = True

    return changed


def _init_state(campaign: Campaign) -> CampaignState:
    msg_idxs = list(range(len(campaign.message_links)))
    tgt_idxs = list(range(len(campaign.target_refs)))

    return CampaignState(
        campaign_id=campaign.id,
        msg_bag=msg_idxs[:],
        tgt_bag=tgt_idxs[:],
        msg_rr_idx=0,
        tgt_rr_idx=0,
        sent_in_current_batch=0,
        next_at=None,
        start_at=datetime.now(),
        sent_total=0,
        paused=False,
        paused_at=None,
        last_source_message_ids={},
        target_fail_counts={},
        target_disabled={},
    )


def _pick_index_shuffle_bag(rng: random.Random, bag: List[int], universe: List[int]) -> int:
    if not universe:
        raise ValueError("Empty universe")
    if not bag:
        bag.extend(universe)
        rng.shuffle(bag)
    return bag.pop()


def _pick_index_round_robin(idx: int, universe: List[int]) -> Tuple[int, int]:
    if not universe:
        raise ValueError("Empty universe")
    if idx < 0 or idx >= len(universe):
        idx = 0
    v = universe[idx]
    idx = (idx + 1) % len(universe)
    return v, idx


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _is_transient_error(e: Exception) -> bool:
    name = type(e).__name__.lower()
    if "timeout" in name:
        return True
    if "connection" in name or "network" in name:
        return True
    if "server" in name or "rpc" in name:
        return True
    return False


def _is_slowmode_error(e: Exception) -> bool:
    name = type(e).__name__.lower()
    return "slowmode" in name


def _is_db_locked_error(e: Exception) -> bool:
    if isinstance(e, sqlite3.OperationalError):
        return "database is locked" in str(e).lower()
    return "database is locked" in str(e).lower()


def _is_disconnected_error(e: Exception) -> bool:
    text = f"{type(e).__name__}: {e}".lower()
    return (
        "cannot send requests while disconnected" in text
        or "while disconnected" in text
        or "connection reset" in text
        or "connection aborted" in text
        or "connection closed" in text
    )


async def _ensure_client_connected(tg_client) -> None:
    try:
        if tg_client.is_connected():
            return
    except Exception:
        pass
    await tg_client.connect()


async def _reconnect_client(tg_client) -> None:
    try:
        if tg_client.is_connected():
            await tg_client.disconnect()
    except Exception:
        pass
    await tg_client.connect()


def _schedule_next_send(
    state: CampaignState,
    rng: random.Random,
    campaign: Campaign,
    *,
    settings: AdvancedSettings,
    rate_multiplier: float,
    extra_multiplier: float = 1.0,
    extra_delay_sec: int = 0,
) -> None:
    gap = _rand_between(rng, campaign.send_gap_min_sec, campaign.send_gap_max_sec)
    gap = max(gap, int(settings.global_min_send_gap_sec or 1))
    if extra_delay_sec and extra_delay_sec > 0:
        gap += int(extra_delay_sec)
    if extra_multiplier and extra_multiplier > 1.0:
        gap = int(gap * extra_multiplier)
    if rate_multiplier and rate_multiplier > 0:
        gap = int(gap * rate_multiplier)
    jitter_min = int(getattr(settings, "global_jitter_min_sec", 0) or 0)
    jitter_max = int(getattr(settings, "global_jitter_max_sec", 0) or 0)
    if jitter_max < jitter_min:
        jitter_min, jitter_max = jitter_max, jitter_min
    if jitter_max > 0:
        gap += _rand_between(rng, jitter_min, jitter_max)
    state.next_at = datetime.now() + timedelta(seconds=gap)


def _finish_cycle_if_needed(
    *,
    state: CampaignState,
    batch_size: int,
    log_path: Path,
) -> None:
    if state.sent_in_current_batch >= batch_size:
        state.sent_in_current_batch = 0
        _log_line(log_path, "TARGET_CYCLE_COMPLETE")


def _is_topic_closed_error(e: Exception) -> bool:
    s = str(e).lower()
    return "topic_closed" in s or "topic closed" in s


def _is_not_in_forum_error(e: Exception) -> bool:
    s = str(e).lower()
    return "topic_id_invalid" in s or "msg_id_invalid" in s or "forum" in s


def _is_no_permission_error(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return (
        "chatwriteforbidden" in s
        or "chatadminrequired" in s
        or "userbannedinchannel" in s
        or "forbidden" in s
        or "not enough rights" in s
        or "write" in s and "forbidden" in s
    )


def _is_dead_group_error(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return (
        "channelprivate" in s
        or "channelinvalid" in s
        or "chatidinvalid" in s
        or "peeridinvalid" in s
        or "usernameinvalid" in s
        or "usernamenotoccupied" in s
        or "entitynotfound" in s
        or "chatinvalid" in s
    )


def _is_media_restriction_error(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".casefold()
    markers = (
        "chatsendmediaforbidden",
        "chatsendphotosforbidden",
        "chatsendvideosforbidden",
        "chatsendgifsforbidden",
        "chatsendstickersforbidden",
        "chatsenddocsforbidden",
        "chatsendaudiosforbidden",
        "chatsendvoicesforbidden",
        "chatsendroundvideosforbidden",
        "chatsendpollforbidden",
        "media forbidden",
        "not allowed to send media",
        "not allowed to send photos",
        "not allowed to send videos",
    )
    return any(marker in text for marker in markers)


def _permanent_target_error(e: Exception) -> tuple[str, str] | None:
    if _is_media_restriction_error(e):
        return (
            "media_not_allowed",
            "This destination does not allow the photo, video, or media in this ad.",
        )
    if _is_topic_closed_error(e) or _is_not_in_forum_error(e):
        return ("topic_unavailable", "The selected topic is closed or no longer available.")
    if _is_dead_group_error(e):
        return (
            "destination_unavailable",
            "This destination is no longer accessible. The account may have left it or been removed.",
        )
    if _is_no_permission_error(e):
        return (
            "cannot_send",
            "The account no longer has permission to send messages to this destination.",
        )
    return None


def _friendly_send_error(e: Exception) -> str:
    name = type(e).__name__
    text = f"{name} {e}".casefold()
    if _is_disconnected_error(e):
        return "The Telegram connection was interrupted. The destination was kept and will be retried."
    if _is_slowmode_error(e):
        seconds = getattr(e, "seconds", None)
        wait = f" in {int(seconds)} seconds" if isinstance(seconds, int) and seconds > 0 else " later"
        return f"Slow mode is active. The destination was kept and will be retried{wait}."
    if "messageidinvalid" in text or "message id invalid" in text:
        return "The source message was deleted or is no longer available. The destination was kept."
    if "chatforwardsrestricted" in text or "forwards restricted" in text:
        return "Telegram protects this source from forwarding. Use a different source message."
    return f"Telegram could not send this message ({name}). The destination was kept and will be retried."


def _friendly_source_error(source: str, e: Exception) -> str:
    text = f"{type(e).__name__} {e}".casefold()
    if "no user has" in text and "as username" in text:
        return (
            f"The username in source {source} no longer exists or has changed. "
            "Update this ad with the current message link. Destinations were not affected."
        )
    if "private" in text or "forbidden" in text or "not found" in text:
        return f"Source {source} is not accessible to the logged-in Telegram account."
    if _is_disconnected_error(e):
        return f"Telegram disconnected while reading source {source}. It will retry automatically."
    return f"Source {source} could not be read ({type(e).__name__}). It will retry automatically."


def _is_retryable_source_error(e: Exception) -> bool:
    return (
        isinstance(e, FloodWaitError)
        or isinstance(e, asyncio.TimeoutError)
        or _is_disconnected_error(e)
        or _is_transient_error(e)
        or _is_db_locked_error(e)
    )


async def _resolve_source_inputs(
    tg_client,
    *,
    source_ref,
    source_label: str,
    cache_key: str,
    entity_cache: Dict[str, Any],
    input_cache: Dict[str, Any],
) -> Tuple[Any, Any]:
    try:
        entity = entity_cache.get(cache_key)
        if entity is None:
            entity = await resolve_source_entity(tg_client, source_ref)
            entity_cache[cache_key] = entity
            input_cache.pop(cache_key, None)
        input_entity = input_cache.get(cache_key)
        if input_entity is None:
            input_entity = await tg_client.get_input_entity(entity)
            input_cache[cache_key] = input_entity
        return entity, input_entity
    except Exception as e:
        if _is_retryable_source_error(e):
            raise
        raise _SourceUnavailableError(source_label, e) from e


def _target_key(t: dict) -> str:
    gid = int(t.get("group_id"))
    tid = t.get("topic_id", None)
    tid = int(tid) if tid is not None else 0
    return f"{gid}:{tid}"


def _warmup_multiplier(state: CampaignState, campaign: Campaign) -> float:
    if not bool(getattr(campaign, "warmup_enabled", False)):
        return 1.0
    minutes = getattr(campaign, "warmup_minutes", None)
    if not isinstance(minutes, int) or minutes <= 0:
        return 1.0
    start_at = getattr(state, "start_at", None)
    if not isinstance(start_at, datetime):
        return 1.0
    elapsed = (datetime.now() - start_at).total_seconds()
    total = max(1.0, float(minutes) * 60.0)
    t = min(1.0, max(0.0, elapsed / total))
    start_mult = float(getattr(campaign, "warmup_start_multiplier", 2.0) or 2.0)
    end_mult = float(getattr(campaign, "warmup_end_multiplier", 1.0) or 1.0)
    mult = start_mult + (end_mult - start_mult) * t
    return max(1.0, float(mult))


def _backoff_multiplier(state: CampaignState, campaign: Campaign, settings: AdvancedSettings) -> float:
    if not bool(getattr(campaign, "adaptive_backoff_enabled", True)):
        return 1.0
    streak = int(getattr(state, "error_streak", 0) or 0)
    if streak <= 0:
        return 1.0
    step = float(getattr(settings, "backoff_step", 0.5) or 0.5)
    max_mult = float(getattr(settings, "backoff_max_multiplier", 4.0) or 4.0)
    mult = 1.0 + (streak * step)
    return min(max_mult, max(1.0, mult))


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _is_target_disabled(state: CampaignState, key: str, now: datetime) -> bool:
    disabled = (state.target_disabled or {}).get(key)
    if not disabled:
        return False
    until = _parse_iso_dt(disabled.get("until"))
    if until and now >= until:
        state.target_disabled.pop(key, None)
        return False
    return True


def _disable_target(
    *,
    state: CampaignState,
    key: str,
    reason: str,
    settings: AdvancedSettings,
    now: datetime,
) -> None:
    state.target_disabled = state.target_disabled or {}
    minutes = getattr(settings, "auto_disable_target_minutes", None)
    until = None
    if isinstance(minutes, int) and minutes > 0:
        until = (now + timedelta(minutes=minutes)).isoformat()
    state.target_disabled[key] = {"reason": reason, "until": until}


def _record_target_failure(
    *,
    state: CampaignState,
    key: str,
    reason: str,
    settings: AdvancedSettings,
    now: datetime,
) -> bool:
    state.target_fail_counts = state.target_fail_counts or {}
    count = int(state.target_fail_counts.get(key, 0)) + 1
    state.target_fail_counts[key] = count
    threshold = int(getattr(settings, "auto_disable_target_error_count", 3) or 3)
    if threshold > 0 and count >= threshold:
        _disable_target(state=state, key=key, reason=reason, settings=settings, now=now)
        return True
    return False


async def _wait_with_live_ticks(
    *,
    state: CampaignState,
    state_path: Optional[Path] = None,
    on_event: Optional[EventCb],
    last_target_label: Optional[str],
    last_link: Optional[str],
) -> None:
    """
    Emit wait events every 1 second, so the UI has continuous live updates.
    """
    if state.next_at is None:
        return

    while True:
        if state_path is not None:
            reloaded = load_state(state_path, state.campaign_id)
            if reloaded is not None:
                state = reloaded
                if getattr(state, "stopped", False) or getattr(state, "paused", False):
                    return

        now = datetime.now()
        if state.next_at <= now:
            return

        remaining = max(0, int((state.next_at - now).total_seconds()))
        _emit(
            on_event,
            {
                "type": "wait",
                "target": last_target_label,
                "link": last_link,
                "info": None,
                "sent_total": getattr(state, "sent_total", 0),
                "sent_in_batch": getattr(state, "sent_in_current_batch", 0),
                "next_at": state.next_at,
                "next_in_sec": remaining,
            },
        )

        # sleep 1s ticks, but never oversleep past next_at
        step = 1.0
        left = (state.next_at - datetime.now()).total_seconds()
        await asyncio.sleep(max(0.1, min(step, max(0.0, left))))


async def run_campaign(
    *,
    tg_client,
    campaign: Campaign,
    state_path: Path,
    dry_run: bool,
    seed: Optional[int] = None,
    on_event: Optional[EventCb] = None,
    settings: Optional[AdvancedSettings] = None,
    account_rate_multiplier: float = 1.0,
    account_schedule: Optional[Dict[str, Any]] = None,
    reconnect_minutes: Optional[int] = None,
    send_coordinator: Optional[DestinationSendCoordinator] = None,
) -> None:
    rng = random.Random(seed)
    log_path = LOGS_DIR / f"campaign_{campaign.id}.log"
    stars_map = _load_stars_map()
    settings = settings or load_settings()
    send_coordinator = send_coordinator or DestinationSendCoordinator()
    last_reconnect_at: Optional[datetime] = None
    use_latest = bool(getattr(campaign, "use_latest_source", False))
    latest_sources = list(getattr(campaign, "latest_sources", []) or [])
    source_entity_cache: Dict[str, Any] = {}
    source_input_cache: Dict[str, Any] = {}
    source_peer_cache: Dict[str, Any] = {}
    source_error_notified: set[str] = set()

    if not getattr(campaign, "enabled", True):
        _log_line(log_path, f"STOP disabled campaign={campaign.name} id={campaign.id}")
        _emit(on_event, {"type": "stop", "info": "disabled"})
        return

    state = load_state(state_path, campaign.id)
    if state is None:
        state = _init_state(campaign)
        save_state(state_path, state)
    if state.start_at is None:
        state.start_at = datetime.now()
        save_state(state_path, state)
    if _sanitize_runtime_state(state, campaign, latest_sources=latest_sources, use_latest=use_latest):
        _log_line(log_path, f"STATE_SANITIZED campaign={campaign.name} id={campaign.id}")
        save_state(state_path, state)
    if getattr(state, "stopped", False):
        _log_line(log_path, f"STOP flagged_stopped campaign={campaign.name} id={campaign.id}")
        _emit(on_event, {"type": "stop", "info": "stopped"})
        return

    if use_latest:
        if not latest_sources:
            _log_line(log_path, f"STOP no_sources campaign={campaign.name} id={campaign.id}")
            _emit(on_event, {"type": "stop", "info": "no_sources"})
            return
        msg_universe: list[int] = []
    else:
        msg_universe = list(range(len(campaign.message_links)))
        if not msg_universe:
            _log_line(log_path, f"STOP no_messages campaign={campaign.name} id={campaign.id}")
            _emit(on_event, {"type": "stop", "info": "no_messages"})
            return

    tgt_universe = list(range(len(campaign.target_refs)))

    if not tgt_universe:
        _log_line(log_path, f"STOP no_targets targets_total={len(campaign.target_refs)}")
        _emit(on_event, {"type": "stop", "info": "no_targets"})
        return

    batch_size = max(1, len(tgt_universe))

    # Clean bags to current universes
    state.msg_bag = [i for i in state.msg_bag if i in msg_universe]
    state.tgt_bag = [i for i in state.tgt_bag if i in tgt_universe]

    target_cooldown_until: Dict[int, datetime] = {}

    def _requeue_selection(target_index: int, message_index: Optional[int]) -> None:
        if campaign.target_strategy == "shuffle_bag" and target_index not in state.tgt_bag:
            state.tgt_bag.append(target_index)
        if use_latest or message_index is None:
            return
        if campaign.message_strategy == "shuffle_bag":
            if message_index not in state.msg_bag:
                state.msg_bag.append(message_index)
            return
        try:
            state.msg_rr_idx = msg_universe.index(message_index)
        except ValueError:
            pass

    def _remove_target_permanently(target: dict) -> bool:
        nonlocal tgt_universe, target_cooldown_until, batch_size
        group_id = int(target.get("group_id"))
        raw_topic = target.get("topic_id")
        topic_id = int(raw_topic) if raw_topic is not None else None
        target_key = _target_key(target)
        remove_campaign_target(campaign.id, group_id, topic_id)
        previous_count = len(campaign.target_refs)
        campaign.target_refs = [
            item for item in campaign.target_refs if _target_key(item) != target_key
        ]
        removed = len(campaign.target_refs) < previous_count
        tgt_universe = list(range(len(campaign.target_refs)))
        batch_size = max(1, len(tgt_universe))
        target_cooldown_until = {}
        state.tgt_bag = []
        state.tgt_rr_idx = 0
        state.target_fail_counts = state.target_fail_counts or {}
        state.target_disabled = state.target_disabled or {}
        state.target_fail_counts.pop(target_key, None)
        state.target_disabled.pop(target_key, None)
        return removed

    # Keep last selected target/link so wait/next always have context
    last_target_label: Optional[str] = None
    last_link: Optional[str] = None

    _log_line(
        log_path,
        f"START campaign={campaign.name} id={campaign.id} dry_run={dry_run} targets_total={len(campaign.target_refs)}",
    )

    _emit(
        on_event,
        {
            "type": "start",
            "dry_run": dry_run,
            "targets_total": len(campaign.target_refs),
            "batch_size": batch_size,
            "send_gap_min_sec": campaign.send_gap_min_sec,
            "send_gap_max_sec": campaign.send_gap_max_sec,
            "batch_gap_min_sec": campaign.batch_gap_min_sec,
            "batch_gap_max_sec": campaign.batch_gap_max_sec,
            "schedule_days": getattr(campaign, "schedule_days", "all"),
            "schedule_windows": getattr(campaign, "schedule_windows", None),
            "schedule_windows_weekday": getattr(campaign, "schedule_windows_weekday", None),
            "schedule_windows_weekend": getattr(campaign, "schedule_windows_weekend", None),
            "sleep_start": getattr(campaign, "sleep_start", "00:00"),
            "sleep_end": getattr(campaign, "sleep_end", "07:00"),
            "message_strategy": campaign.message_strategy,
            "target_strategy": campaign.target_strategy,
            "warmup_enabled": getattr(campaign, "warmup_enabled", False),
            "warmup_minutes": getattr(campaign, "warmup_minutes", None),
            "warmup_start_multiplier": getattr(campaign, "warmup_start_multiplier", 2.0),
            "warmup_end_multiplier": getattr(campaign, "warmup_end_multiplier", 1.0),
            "adaptive_backoff_enabled": getattr(campaign, "adaptive_backoff_enabled", True),
            "sent_total": state.sent_total,
            "sent_in_batch": state.sent_in_current_batch,
            "next_at": state.next_at,
            "next_in_sec": None,
        },
    )

    async def _get_latest_from_source(raw: str) -> Optional[Tuple[str, int]]:
        norm = _normalize_source_ref(raw)
        if not norm:
            return None
        peer_ref = _coerce_source_peer(norm)
        key = _source_key(peer_ref)
        source_peer_cache[key] = peer_ref
        try:
            await _ensure_client_connected(tg_client)
            entity = source_entity_cache.get(key)
            if entity is None:
                entity = await tg_client.get_entity(peer_ref)
                source_entity_cache[key] = entity
                source_input_cache.pop(key, None)
            msgs = await tg_client.get_messages(entity, limit=1)
        except Exception as e:
            _log_line(log_path, f"SOURCE_ERROR ref={raw} err={type(e).__name__}: {e}")
            if key not in source_error_notified:
                source_error_notified.add(key)
                _emit(
                    on_event,
                    {
                        "type": "source_error",
                        "source": raw,
                        "error": _friendly_source_error(raw, e),
                    },
                )
            return None
        source_error_notified.discard(key)
        if not msgs:
            return None
        try:
            msg = msgs[0]
        except Exception:
            return None
        msg_id = getattr(msg, "id", None)
        if msg_id is None:
            return None
        return key, int(msg_id)

    async def _select_latest_source_message() -> Optional[Tuple[str, int]]:
        if not latest_sources:
            return None
        start = int(getattr(state, "src_rr_idx", 0) or 0)
        total = len(latest_sources)
        strategy = str(getattr(campaign, "latest_source_strategy", "round_robin") or "round_robin")
        if strategy == "shuffle_bag":
            source_indexes = list(range(total))
            rng.shuffle(source_indexes)
        else:
            source_indexes = [(start + offset) % total for offset in range(total)]
        for idx in source_indexes:
            raw = latest_sources[idx]
            res = await _get_latest_from_source(raw)
            if not res:
                continue
            key, msg_id = res
            last_sent = (state.last_source_message_ids or {}).get(key)
            if last_sent is not None and int(last_sent) == int(msg_id):
                continue
            state.src_rr_idx = (idx + 1) % total
            state.current_source_id = key
            state.current_source_msg_id = int(msg_id)
            return key, int(msg_id)
        return None

    def _schedule_gate(now: datetime) -> tuple[bool, Optional[str], Optional[datetime]]:
        if getattr(settings, "global_quiet_hours_enabled", False):
            if not is_allowed_now(
                now,
                days_mode="all",
                windows=None,
                sleep_start=getattr(settings, "global_quiet_start", ""),
                sleep_end=getattr(settings, "global_quiet_end", ""),
            ):
                next_ok = next_allowed_time(
                    now,
                    days_mode="all",
                    windows=None,
                    sleep_start=getattr(settings, "global_quiet_start", ""),
                    sleep_end=getattr(settings, "global_quiet_end", ""),
                )
                return False, "global_quiet_hours", next_ok

        if account_schedule:
            if not is_allowed_now(
                now,
                days_mode=account_schedule.get("days_mode", "all"),
                windows=account_schedule.get("windows"),
                sleep_start=account_schedule.get("sleep_start", ""),
                sleep_end=account_schedule.get("sleep_end", "00:00"),
            ):
                next_ok = next_allowed_time(
                    now,
                    days_mode=account_schedule.get("days_mode", "all"),
                    windows=account_schedule.get("windows"),
                    sleep_start=account_schedule.get("sleep_start", ""),
                    sleep_end=account_schedule.get("sleep_end", "00:00"),
                )
                return False, "account_schedule", next_ok

        sched_days = getattr(campaign, "schedule_days", "all")
        sched_windows = getattr(campaign, "schedule_windows", None)
        sched_windows_weekday = getattr(campaign, "schedule_windows_weekday", None)
        sched_windows_weekend = getattr(campaign, "schedule_windows_weekend", None)
        sleep_start = getattr(campaign, "sleep_start", "00:00")
        sleep_end = getattr(campaign, "sleep_end", "07:00")
        if not is_allowed_now(
            now,
            days_mode=sched_days,
            windows=sched_windows,
            sleep_start=sleep_start,
            sleep_end=sleep_end,
            windows_weekday=sched_windows_weekday,
            windows_weekend=sched_windows_weekend,
        ):
            next_ok = next_allowed_time(
                now,
                days_mode=sched_days,
                windows=sched_windows,
                sleep_start=sleep_start,
                sleep_end=sleep_end,
                windows_weekday=sched_windows_weekday,
                windows_weekend=sched_windows_weekend,
            )
            return False, "outside_schedule_or_sleep", next_ok

        return True, None, None

    while True:
        reloaded = load_state(state_path, campaign.id)
        if reloaded is not None:
            state = reloaded
        if getattr(state, "stopped", False):
            _log_line(log_path, f"STOP flagged_stopped campaign={campaign.name} id={campaign.id}")
            _emit(on_event, {"type": "stop", "info": "stopped"})
            return

        now = datetime.now()

        # auto-resume if configured
        if getattr(state, "paused", False) and getattr(state, "paused_reason", None) == "auto":
            auto_minutes = getattr(settings, "auto_resume_minutes", None)
            if auto_minutes and state.paused_at:
                if now >= state.paused_at + timedelta(minutes=int(auto_minutes)):
                    state.paused = False
                    state.paused_at = None
                    state.paused_reason = None
                    save_state(state_path, state)
        if getattr(state, "paused", False) and getattr(state, "paused_reason", None) == "schedule":
            allowed, _, _ = _schedule_gate(now)
            if allowed:
                state.paused = False
                state.paused_at = None
                state.paused_reason = None
                state.next_at = now
                save_state(state_path, state)

        if getattr(state, "paused", False):
            _emit(on_event, {"type": "paused", "info": "paused", "sent_total": state.sent_total})
            await asyncio.sleep(1)
            continue

        if state.next_at is None:
            state.next_at = now
            save_state(state_path, state)

        # enforce hourly/daily caps
        today = now.strftime("%d/%m/%Y")
        if getattr(state, "day_date", None) != today:
            state.day_date = today
            state.day_sent_count = 0
        if getattr(state, "per_target_day_date", None) != today:
            state.per_target_day_date = today
            state.per_target_day_counts = {}
        if state.hour_window_start is None or (now - state.hour_window_start) >= timedelta(hours=1):
            state.hour_window_start = now
            state.hour_sent_count = 0

        # global max msgs/hour
        global_max = getattr(settings, "global_max_msgs_per_hour", None)
        if isinstance(global_max, int) and global_max > 0:
            if state.hour_sent_count >= global_max:
                next_hour = (state.hour_window_start or now) + timedelta(hours=1)
                state.next_at = next_hour
                save_state(state_path, state)
                _emit(
                    on_event,
                    {
                        "type": "schedule_wait",
                        "target": last_target_label,
                        "link": last_link,
                        "info": "global_hourly_cap_reached",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": state.next_at,
                        "next_in_sec": max(0, int((state.next_at - now).total_seconds())),
                    },
                )
                await _wait_with_live_ticks(
                    state=state,
                    state_path=state_path,
                    on_event=on_event,
                    last_target_label=last_target_label,
                    last_link=last_link,
                )
                continue

        if isinstance(getattr(campaign, "daily_cap", None), int) and campaign.daily_cap is not None:
            if state.day_sent_count >= campaign.daily_cap:
                next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                state.next_at = next_day
                save_state(state_path, state)
                _emit(
                    on_event,
                    {
                        "type": "schedule_wait",
                        "target": last_target_label,
                        "link": last_link,
                        "info": "daily_cap_reached",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": state.next_at,
                        "next_in_sec": max(0, int((state.next_at - now).total_seconds())),
                    },
                )
                await _wait_with_live_ticks(
                    state=state,
                    state_path=state_path,
                    on_event=on_event,
                    last_target_label=last_target_label,
                    last_link=last_link,
                )
                continue

        if isinstance(getattr(campaign, "max_msgs_per_hour", None), int) and campaign.max_msgs_per_hour is not None:
            if state.hour_sent_count >= campaign.max_msgs_per_hour:
                next_hour = (state.hour_window_start or now) + timedelta(hours=1)
                state.next_at = next_hour
                save_state(state_path, state)
                _emit(
                    on_event,
                    {
                        "type": "schedule_wait",
                        "target": last_target_label,
                        "link": last_link,
                        "info": "hourly_cap_reached",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": state.next_at,
                        "next_in_sec": max(0, int((state.next_at - now).total_seconds())),
                    },
                )
                await _wait_with_live_ticks(
                    state=state,
                    state_path=state_path,
                    on_event=on_event,
                    last_target_label=last_target_label,
                    last_link=last_link,
                )
                continue

        # Scheduler gate: honor quiet hours + account + campaign schedule
        allowed, reason, next_ok = _schedule_gate(now)
        if not allowed:
            if next_ok and (state.next_at is None or state.next_at < next_ok):
                state.next_at = next_ok
            state.paused = True
            state.paused_at = datetime.now()
            state.paused_reason = "schedule"
            save_state(state_path, state)
            _emit(
                on_event,
                {
                    "type": "schedule_wait",
                    "target": last_target_label,
                    "link": last_link,
                    "info": reason or "schedule",
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                    "next_at": state.next_at,
                    "next_in_sec": max(0, int((state.next_at - datetime.now()).total_seconds()))
                    if state.next_at
                    else None,
                },
            )
            continue

        # hard stop if next_at drifted too far into the future
        max_drift = getattr(settings, "max_next_at_drift_sec", None)
        if max_drift and state.next_at and (state.next_at - now).total_seconds() > int(max_drift):
            state.stopped = True
            state.stopped_at = datetime.now()
            save_state(state_path, state)
            _log_line(log_path, f"STOP drift_exceeded seconds={max_drift}")
            _emit(on_event, {"type": "stop", "info": "drift_exceeded"})
            return

        if state.next_at > now:
            await _wait_with_live_ticks(
                state=state,
                state_path=state_path,
                on_event=on_event,
                last_target_label=last_target_label,
                last_link=last_link,
            )
            continue

        # reconnect policy
        if reconnect_minutes and reconnect_minutes > 0:
            if last_reconnect_at is None or now >= last_reconnect_at + timedelta(minutes=reconnect_minutes):
                try:
                    await _reconnect_client(tg_client)
                    last_reconnect_at = now
                except Exception:
                    pass

        now = datetime.now()
        warmup_mult = _warmup_multiplier(state, campaign)
        backoff_mult = _backoff_multiplier(state, campaign, settings)
        extra_mult = max(1.0, warmup_mult * backoff_mult)
        available_tis: list[int] = []
        coordinator_deadlines: list[datetime] = []
        coordinator_busy = False
        disabled_count = 0
        for t_index in tgt_universe:
            t = campaign.target_refs[t_index]
            key = _target_key(t)
            if _is_target_disabled(state, key, now):
                disabled_count += 1
                continue
            until = target_cooldown_until.get(t_index)
            if until is not None and until > now:
                continue
            group_id = int(t["group_id"])
            shared_wait = send_coordinator.ready_in_seconds(group_id)
            if shared_wait > 0:
                coordinator_deadlines.append(now + timedelta(seconds=shared_wait))
                continue
            if send_coordinator.is_busy(group_id):
                coordinator_busy = True
                continue
            available_tis.append(t_index)

        if not available_tis:
            if disabled_count >= len(tgt_universe):
                disabled_untils: list[datetime] = []
                for t_index in tgt_universe:
                    key = _target_key(campaign.target_refs[t_index])
                    disabled_meta = (state.target_disabled or {}).get(key)
                    if not isinstance(disabled_meta, dict):
                        continue
                    until_dt = _parse_iso_dt(disabled_meta.get("until"))
                    if until_dt is not None:
                        disabled_untils.append(until_dt)

                if disabled_untils:
                    soonest = min(disabled_untils)
                    wait_s = max(1, int((soonest - now).total_seconds()))
                    state.next_at = now + timedelta(seconds=wait_s)
                    save_state(state_path, state)
                    _log_line(log_path, f"ALL_TARGETS_DISABLED wait_seconds={wait_s}")
                    _emit(
                        on_event,
                        {
                            "type": "schedule_wait",
                            "target": last_target_label,
                            "link": last_link,
                            "info": "all_targets_disabled",
                            "sent_total": state.sent_total,
                            "sent_in_batch": state.sent_in_current_batch,
                            "next_in_sec": wait_s,
                            "next_at": state.next_at,
                        },
                    )
                    await _wait_with_live_ticks(
                        state=state,
                        state_path=state_path,
                        on_event=on_event,
                        last_target_label=last_target_label,
                        last_link=last_link,
                    )
                    continue

                _log_line(log_path, "STOP all_targets_disabled_permanent")
                _emit(on_event, {"type": "stop", "info": "all_targets_disabled_permanent"})
                return
            wait_deadlines = [deadline for deadline in target_cooldown_until.values() if deadline > now]
            wait_deadlines.extend(coordinator_deadlines)
            if coordinator_busy:
                wait_deadlines.append(now + timedelta(seconds=1))
            if not wait_deadlines:
                fallback_wait = max(1, int(getattr(settings, "cooldown_generic_error_sec", 60) or 60))
                state.next_at = now + timedelta(seconds=fallback_wait)
                save_state(state_path, state)
                _log_line(log_path, f"ALL_TARGETS_WAIT_FALLBACK wait_seconds={fallback_wait}")
                await _wait_with_live_ticks(
                    state=state,
                    state_path=state_path,
                    on_event=on_event,
                    last_target_label=last_target_label,
                    last_link=last_link,
                )
                continue
            soonest = min(wait_deadlines)
            wait_s = max(1, int((soonest - now).total_seconds()))
            state.next_at = datetime.now() + timedelta(seconds=wait_s)
            save_state(state_path, state)
            _log_line(log_path, f"ALL_TARGETS_COOLDOWN wait_seconds={wait_s}")
            _emit(
                on_event,
                {
                    "type": "cooldown_all",
                    "target": last_target_label,
                    "link": last_link,
                    "info": f"wait_sec={wait_s}",
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                    "next_in_sec": wait_s,
                    "next_at": state.next_at,
                },
            )
            await _wait_with_live_ticks(
                state=state,
                state_path=state_path,
                on_event=on_event,
                last_target_label=last_target_label,
                last_link=last_link,
            )
            continue

        # Pick target index
        if campaign.target_strategy == "shuffle_bag":
            state.tgt_bag = [i for i in state.tgt_bag if i in available_tis]
            ti = _pick_index_shuffle_bag(rng, state.tgt_bag, available_tis)
        else:
            if state.tgt_rr_idx >= len(available_tis):
                state.tgt_rr_idx = 0
            ti, state.tgt_rr_idx = _pick_index_round_robin(state.tgt_rr_idx, available_tis)
        if ti < 0 or ti >= len(campaign.target_refs):
            state.tgt_bag = []
            state.tgt_rr_idx = 0
            _log_line(log_path, f"STATE_RESET invalid_target_index={ti} targets_total={len(campaign.target_refs)}")
            save_state(state_path, state)
            continue

        # Pick message or latest-source payload
        mi: Optional[int] = None
        if use_latest:
            if state.current_source_id is None or state.current_source_msg_id is None:
                picked = await _select_latest_source_message()
                if not picked:
                    poll_sec = int(getattr(settings, "latest_source_poll_sec", 60) or 60)
                    state.next_at = datetime.now() + timedelta(seconds=poll_sec)
                    save_state(state_path, state)
                    _emit(
                        on_event,
                        {
                            "type": "wait_source",
                            "target": last_target_label,
                            "link": last_link,
                            "info": f"no_new_message poll_sec={poll_sec}",
                            "sent_total": state.sent_total,
                            "sent_in_batch": state.sent_in_current_batch,
                            "next_at": state.next_at,
                            "next_in_sec": poll_sec,
                        },
                    )
                    await _wait_with_live_ticks(
                        state=state,
                        state_path=state_path,
                        on_event=on_event,
                        last_target_label=last_target_label,
                        last_link=last_link,
                    )
                    continue
            msg_link = f"latest:{state.current_source_id}:{state.current_source_msg_id}"
        else:
            if campaign.message_strategy == "shuffle_bag":
                mi = _pick_index_shuffle_bag(rng, state.msg_bag, msg_universe)
            else:
                mi, state.msg_rr_idx = _pick_index_round_robin(state.msg_rr_idx, msg_universe)
            if mi < 0 or mi >= len(campaign.message_links):
                state.msg_bag = []
                state.msg_rr_idx = 0
                _log_line(log_path, f"STATE_RESET invalid_message_index={mi} messages_total={len(campaign.message_links)}")
                save_state(state_path, state)
                continue
            msg_link = campaign.message_links[mi]

        target = campaign.target_refs[ti]
        label = _label_target(target)
        topic_id = _safe_int(target.get("topic_id", None))
        extra_delay_sec = _safe_int(target.get("extra_delay_sec")) or 0
        target_key = _target_key(target)

        # per-target daily cap
        per_cap = getattr(campaign, "per_target_daily_cap", None)
        if isinstance(per_cap, int) and per_cap > 0:
            count = int((state.per_target_day_counts or {}).get(target_key, 0))
            if count >= per_cap:
                next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                target_cooldown_until[ti] = next_day
                _requeue_selection(ti, mi)
                _emit(
                    on_event,
                    {
                        "type": "schedule_wait",
                        "target": label,
                        "link": msg_link,
                        "info": "per_target_daily_cap_reached",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": next_day,
                        "next_in_sec": max(0, int((next_day - now).total_seconds())),
                    },
                )
                save_state(state_path, state)
                continue

        # store context for wait/next
        last_target_label = label
        last_link = msg_link

        _emit(
            on_event,
            {
                "type": "selected",
                "target": label,
                "link": msg_link,
                "info": "selected",
                "sent_total": state.sent_total,
                "sent_in_batch": state.sent_in_current_batch,
            },
        )

        group_id = int(target["group_id"])
        destination_peer = _target_peer_ref(target)
        stars_cost = stars_map.get(group_id, None)

        if dry_run:
            _log_line(log_path, f"DRY_SEND target={label} link={msg_link}")

            state.sent_in_current_batch += 1
            state.sent_total += 1
            state.hour_sent_count = int(getattr(state, "hour_sent_count", 0) or 0) + 1
            state.day_sent_count = int(getattr(state, "day_sent_count", 0) or 0) + 1
            state.per_target_day_counts = state.per_target_day_counts or {}
            state.per_target_day_counts[target_key] = int(state.per_target_day_counts.get(target_key, 0)) + 1
            state.last_success_at = datetime.now()
            if getattr(state, "error_streak", 0) > 0:
                state.error_streak = max(0, int(state.error_streak) - 1)
            if campaign.per_target_cooldown_sec:
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=int(campaign.per_target_cooldown_sec))
            # LOG TO HISTORY DATABASE
            try:
                async def _log_success(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    message_link=msg_link,
                    destination_group_id=group_id,
                    group_title=target.get("group_title", "Unknown"),
                    destination_topic_id=topic_id,
                    topic_title=target.get("topic_title"),
                    send_stars_cost=stars_cost or 0,
                ):
                    history = await get_history()
                    await history.log_send(
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        message_link=message_link,
                        group_id=destination_group_id,
                        group_title=group_title,
                        topic_id=destination_topic_id,
                        topic_title=topic_title,
                        success=True,
                        stars_cost=send_stars_cost,
                    )
                await _retry_if_db_locked(_log_success, tries=6, base_delay=0.5)
            except Exception:
                pass  # Don't break campaign if logging fails

            _emit(
                on_event,
                {
                    "type": "dry_send",
                    "target": label,
                    "link": msg_link,
                    "info": None,
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                },
            )

            _schedule_next_send(
                state,
                rng,
                campaign,
                settings=settings,
                rate_multiplier=account_rate_multiplier,
                extra_multiplier=extra_mult,
                extra_delay_sec=extra_delay_sec,
            )
            _finish_cycle_if_needed(
                state=state,
                batch_size=batch_size,
                log_path=log_path,
            )
            if use_latest and state.sent_in_current_batch == 0 and state.current_source_id is not None:
                state.last_source_message_ids = state.last_source_message_ids or {}
                state.last_source_message_ids[state.current_source_id] = int(state.current_source_msg_id or 0)
                state.current_source_id = None
                state.current_source_msg_id = None

            next_in = max(0, int((state.next_at - datetime.now()).total_seconds())) if state.next_at else None
            _emit(
                on_event,
                {
                    "type": "next",
                    "target": last_target_label,
                    "link": last_link,
                    "info": None,
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                    "next_at": state.next_at,
                    "next_in_sec": next_in,
                },
            )

            save_state(state_path, state)
            continue

        # LIVE SEND
        lease = await send_coordinator.try_acquire(group_id)
        if lease is None:
            _requeue_selection(ti, mi)
            state.next_at = datetime.now() + timedelta(milliseconds=250)
            save_state(state_path, state)
            continue

        active_source_key: Optional[str] = None

        async def _perform_live_send() -> None:
            nonlocal active_source_key
            if use_latest:
                source_key = state.current_source_id
                src_msg_id = state.current_source_msg_id
                if source_key is None or src_msg_id is None:
                    raise ValueError("Latest-source not selected")
                src_peer = source_peer_cache.get(source_key)
                if src_peer is None:
                    for raw in latest_sources:
                        norm = _normalize_source_ref(raw)
                        if not norm:
                            continue
                        peer_ref = _coerce_source_peer(norm)
                        if _source_key(peer_ref) == source_key:
                            src_peer = peer_ref
                            source_peer_cache[source_key] = peer_ref
                            break
                if src_peer is None:
                    raise ValueError("Latest-source peer not resolved")
                active_source_key = source_key
            else:
                try:
                    src_peer, src_msg_id = _parse_message_link(msg_link)
                except Exception as e:
                    raise _SourceUnavailableError(msg_link, e) from e
                active_source_key = _source_key(src_peer)

            async def _do_forward() -> None:
                await _ensure_client_connected(tg_client)
                key = active_source_key or _source_key(src_peer)
                src_entity, src_input = await _resolve_source_inputs(
                    tg_client,
                    source_ref=src_peer,
                    source_label=msg_link,
                    cache_key=key,
                    entity_cache=source_entity_cache,
                    input_cache=source_input_cache,
                )
                dest_input = await tg_client.get_input_entity(destination_peer)

                if topic_id is not None:
                    req = ForwardMessagesRequest(
                        from_peer=src_input,
                        id=[int(src_msg_id)],
                        to_peer=dest_input,
                        drop_author=False,
                        drop_media_captions=False,
                        with_my_score=False,
                        random_id=[rng.randint(-2**63, 2**63 - 1)],
                        top_msg_id=int(topic_id),
                    )
                    await tg_client(req)
                else:
                    await tg_client.forward_messages(dest_input, int(src_msg_id), src_entity)

            retries = int(getattr(settings, "retry_transient_count", 0) or 0)
            base_delay = float(getattr(settings, "retry_transient_base_delay_sec", 2.0) or 2.0)
            for attempt in range(retries + 1):
                try:
                    await asyncio.wait_for(_retry_if_db_locked(_do_forward), timeout=60)
                    break
                except asyncio.TimeoutError:
                    if attempt >= retries:
                        raise
                    await asyncio.sleep(base_delay * (attempt + 1))
                except Exception as e:
                    if attempt >= retries or not (_is_transient_error(e) or _is_db_locked_error(e)):
                        raise
                    if _is_disconnected_error(e) or _is_transient_error(e):
                        try:
                            await _reconnect_client(tg_client)
                        except Exception:
                            pass
                    await asyncio.sleep(base_delay * (attempt + 1))

        try:
            async with lease:
                await _perform_live_send()
                send_coordinator.record_success(group_id)

            if active_source_key is not None:
                source_error_notified.discard(active_source_key)

            _log_line(log_path, f"SENT target={label} link={msg_link}")

            state.sent_in_current_batch += 1
            state.sent_total += 1
            state.hour_sent_count = int(getattr(state, "hour_sent_count", 0) or 0) + 1
            state.day_sent_count = int(getattr(state, "day_sent_count", 0) or 0) + 1
            state.per_target_day_counts = state.per_target_day_counts or {}
            state.per_target_day_counts[target_key] = int(state.per_target_day_counts.get(target_key, 0)) + 1
            state.last_success_at = datetime.now()
            if getattr(state, "error_streak", 0) > 0:
                state.error_streak = max(0, int(state.error_streak) - 1)
            if campaign.per_target_cooldown_sec:
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=int(campaign.per_target_cooldown_sec))

            # LOG TO HISTORY DATABASE
            try:
                async def _log_success(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    message_link=msg_link,
                    destination_group_id=group_id,
                    group_title=target.get("group_title", "Unknown"),
                    destination_topic_id=topic_id,
                    topic_title=target.get("topic_title"),
                    send_stars_cost=stars_cost or 0,
                ):
                    history = await get_history()
                    await history.log_send(
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        message_link=message_link,
                        group_id=destination_group_id,
                        group_title=group_title,
                        topic_id=destination_topic_id,
                        topic_title=topic_title,
                        success=True,
                        stars_cost=send_stars_cost,
                    )
                await _retry_if_db_locked(_log_success, tries=6, base_delay=0.5)
            except Exception:
                pass  # Don't break campaign if logging fails

            _emit(
                on_event,
                {
                    "type": "sent",
                    "target": label,
                    "link": msg_link,
                    "info": (f"topic_id={topic_id}" if topic_id is not None else None),
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                },
            )

        except asyncio.TimeoutError:
            state.error_streak = int(getattr(state, "error_streak", 0) or 0) + 1
            state.last_error_at = datetime.now()
            _log_line(log_path, f"ERROR_TIMEOUT target={label} link={msg_link}")
            timeout_cd = int(getattr(settings, "cooldown_timeout_sec", 120) or 120)
            target_cooldown_until[ti] = datetime.now() + timedelta(seconds=timeout_cd)
            _emit(
                on_event,
                {
                    "type": "error",
                    "target": label,
                    "link": msg_link,
                    "error": "Timeout while forwarding (60s).",
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                },
            )
            if not getattr(settings, "continue_on_target_error", True):
                max_streak = getattr(settings, "max_error_streak_pause", None)
                if max_streak and state.error_streak >= int(max_streak):
                    state.paused = True
                    state.paused_at = datetime.now()
                    state.paused_reason = "auto"
                    _log_line(log_path, f"PAUSE auto_error_streak={state.error_streak}")
                    save_state(state_path, state)
                    continue
                kill_streak = getattr(settings, "kill_switch_error_streak", None)
                if kill_streak and state.error_streak >= int(kill_streak):
                    state.stopped = True
                    state.stopped_at = datetime.now()
                    save_state(state_path, state)
                    _log_line(log_path, f"STOP kill_switch_error_streak={state.error_streak}")
                    _emit(on_event, {"type": "stop", "info": "kill_switch_error_streak"})
                    return
            else:
                state.next_at = datetime.now()
                save_state(state_path, state)
                kill_streak = getattr(settings, "kill_switch_error_streak", None)
                if kill_streak and state.error_streak >= int(kill_streak):
                    state.stopped = True
                    state.stopped_at = datetime.now()
                    save_state(state_path, state)
                    _log_line(log_path, f"STOP kill_switch_error_streak={state.error_streak}")
                    _emit(on_event, {"type": "stop", "info": "kill_switch_error_streak"})
                    return
                continue

        except _SourceUnavailableError as e:
            retry_seconds = max(60, int(getattr(settings, "latest_source_poll_sec", 60) or 60))
            state.next_at = datetime.now() + timedelta(seconds=retry_seconds)
            save_state(state_path, state)
            source_key = active_source_key or e.source.casefold()
            _log_line(
                log_path,
                f"SOURCE_ERROR source={e.source} err={type(e.original).__name__}: {e.original}",
            )
            if source_key not in source_error_notified:
                source_error_notified.add(source_key)
                _emit(
                    on_event,
                    {
                        "type": "source_error",
                        "source": e.source,
                        "error": _friendly_source_error(e.source, e.original),
                    },
                )
            continue

        except FloodWaitError as e:
            scope, seconds = send_coordinator.record_flood_wait(
                group_id,
                int(e.seconds) + 1,
            )
            cd_until = datetime.now() + timedelta(seconds=seconds)
            target_cooldown_until[ti] = cd_until
            _requeue_selection(ti, mi)
            state.next_at = cd_until if scope == "global" else datetime.now()
            save_state(state_path, state)

            if scope == "destination":
                _log_line(
                    log_path,
                    f"DESTINATION_WAIT seconds={seconds} target={label} "
                    f"exception={type(e).__name__}",
                )
                _emit(
                    on_event,
                    {
                        "type": "destination_wait",
                        "target": label,
                        "link": msg_link,
                        "info": f"seconds={seconds}",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": cd_until,
                        "next_in_sec": seconds,
                    },
                )
                continue

            _log_line(
                log_path,
                f"ACCOUNT_FLOOD_WAIT_CONFIRMED seconds={seconds} target={label} "
                f"exception={type(e).__name__}",
            )

            _emit(
                on_event,
                {
                    "type": "flood_wait",
                    "target": label,
                    "link": msg_link,
                    "info": f"seconds={seconds}",
                    "sent_total": state.sent_total,
                    "sent_in_batch": state.sent_in_current_batch,
                    "next_at": cd_until,
                    "next_in_sec": seconds,
                },
            )
            continue

        except Exception as e:
            if _is_slowmode_error(e):
                raw_wait = getattr(e, "seconds", None)
                seconds = int(raw_wait) + 1 if isinstance(raw_wait, int) and raw_wait > 0 else 60
                seconds = max(seconds, send_coordinator.ready_in_seconds(group_id))
                cd_until = datetime.now() + timedelta(seconds=seconds)
                target_cooldown_until[ti] = cd_until
                _requeue_selection(ti, mi)
                state.next_at = datetime.now()
                save_state(state_path, state)
                _log_line(log_path, f"SLOW_MODE_DEFERRED seconds={seconds} target={label}")
                _emit(
                    on_event,
                    {
                        "type": "slowmode_wait",
                        "target": label,
                        "link": msg_link,
                        "info": f"seconds={seconds}",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                        "next_at": cd_until,
                        "next_in_sec": seconds,
                    },
                )
                continue

            state.error_streak = int(getattr(state, "error_streak", 0) or 0) + 1
            state.last_error_at = datetime.now()
            # LOG ERROR TO HISTORY
            try:
                async def _log_error(
                    campaign_id=campaign.id,
                    campaign_name=campaign.name,
                    message_link=msg_link,
                    destination_group_id=group_id,
                    group_title=target.get("group_title", "Unknown"),
                    destination_topic_id=_safe_int(target.get("topic_id")),
                    topic_title=target.get("topic_title"),
                    error_type=type(e).__name__,
                    error_message=str(e),
                ):
                    history = await get_history()
                    await history.log_send(
                        campaign_id=campaign_id,
                        campaign_name=campaign_name,
                        message_link=message_link,
                        group_id=destination_group_id,
                        group_title=group_title,
                        topic_id=destination_topic_id,
                        topic_title=topic_title,
                        success=False,
                        error_type=error_type,
                        error_message=error_message,
                    )
                await _retry_if_db_locked(_log_error, tries=6, base_delay=0.5)
            except Exception:
                pass

            if _is_db_locked_error(e):
                cd = max(3, int(getattr(settings, "cooldown_timeout_sec", 120) or 120) // 10)
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=cd)
                _log_line(log_path, f"DB_LOCKED cooldown={cd}s target={label}")
                _emit(
                    on_event,
                    {
                        "type": "error",
                        "target": label,
                        "link": msg_link,
                        "error": f"Database locked; retrying in {cd}s.",
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                    },
                )
                state.next_at = datetime.now() + timedelta(seconds=cd)
                save_state(state_path, state)
                continue

            permanent = _permanent_target_error(e)
            if permanent is not None:
                reason_code, explanation = permanent
                removed = _remove_target_permanently(target)
                state.error_streak = max(0, state.error_streak - 1)
                state.next_at = datetime.now()
                save_state(state_path, state)
                action = "Removed automatically from this ad." if removed else "Already removed from this ad."
                _log_line(
                    log_path,
                    f"REMOVE_TARGET reason={reason_code} target={label} err={type(e).__name__}: {e}",
                )
                _emit(
                    on_event,
                    {
                        "type": "target_removed",
                        "target": label,
                        "link": msg_link,
                        "reason": reason_code,
                        "action": action,
                        "error": explanation,
                        "targets_remaining": len(campaign.target_refs),
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                    },
                )
                if not tgt_universe:
                    _emit(on_event, {"type": "stop", "info": "no_targets"})
                    return
                continue

            if _is_topic_closed_error(e):
                _log_line(log_path, f"ERROR_TOPIC_CLOSED target={label} err={type(e).__name__}: {e}")
                topic_cd = int(getattr(settings, "cooldown_topic_closed_sec", 900) or 900)
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=topic_cd)
                key = _target_key(target)
                disabled = _record_target_failure(
                    state=state,
                    key=key,
                    reason="topic_closed",
                    settings=settings,
                    now=datetime.now(),
                )
                if disabled:
                    _log_line(log_path, f"DISABLE_TARGET reason=topic_closed target={label}")
                _emit(
                    on_event,
                    {
                        "type": "error",
                        "target": label,
                        "link": msg_link,
                        "error": _friendly_send_error(e),
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                    },
                )

            elif _is_not_in_forum_error(e):
                _log_line(log_path, f"ERROR_FORUM_TARGET target={label} err={type(e).__name__}: {e}")
                forum_cd = int(getattr(settings, "cooldown_not_in_forum_sec", 600) or 600)
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=forum_cd)
                key = _target_key(target)
                disabled = _record_target_failure(
                    state=state,
                    key=key,
                    reason="not_in_forum",
                    settings=settings,
                    now=datetime.now(),
                )
                if disabled:
                    _log_line(log_path, f"DISABLE_TARGET reason=not_in_forum target={label}")
                _emit(
                    on_event,
                    {
                        "type": "error",
                        "target": label,
                        "link": msg_link,
                        "error": _friendly_send_error(e),
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                    },
                )

            else:
                _log_line(log_path, f"ERROR target={label} err={type(e).__name__}: {e}")
                generic_cd = int(getattr(settings, "cooldown_generic_error_sec", 60) or 60)
                target_cooldown_until[ti] = datetime.now() + timedelta(seconds=generic_cd)
                key = _target_key(target)
                if _is_no_permission_error(e) and getattr(settings, "auto_disable_on_no_permission", True):
                    _disable_target(
                        state=state,
                        key=key,
                        reason="no_permission",
                        settings=settings,
                        now=datetime.now(),
                    )
                    _log_line(log_path, f"DISABLE_TARGET reason=no_permission target={label}")
                elif _is_dead_group_error(e) and getattr(settings, "auto_disable_on_dead_group", True):
                    _disable_target(
                        state=state,
                        key=key,
                        reason="dead_group",
                        settings=settings,
                        now=datetime.now(),
                    )
                    _log_line(log_path, f"DISABLE_TARGET reason=dead_group target={label}")
                else:
                    disabled = _record_target_failure(
                        state=state,
                        key=key,
                        reason="error",
                        settings=settings,
                        now=datetime.now(),
                    )
                    if disabled:
                        _log_line(log_path, f"DISABLE_TARGET reason=error_count target={label}")
                _emit(
                    on_event,
                    {
                        "type": "error",
                        "target": label,
                        "link": msg_link,
                        "error": _friendly_send_error(e),
                        "sent_total": state.sent_total,
                        "sent_in_batch": state.sent_in_current_batch,
                    },
                )

            if not getattr(settings, "continue_on_target_error", True):
                max_streak = getattr(settings, "max_error_streak_pause", None)
                if max_streak and state.error_streak >= int(max_streak):
                    state.paused = True
                    state.paused_at = datetime.now()
                    state.paused_reason = "auto"
                    _log_line(log_path, f"PAUSE auto_error_streak={state.error_streak}")
                    save_state(state_path, state)
                    continue
                kill_streak = getattr(settings, "kill_switch_error_streak", None)
                if kill_streak and state.error_streak >= int(kill_streak):
                    state.stopped = True
                    state.stopped_at = datetime.now()
                    save_state(state_path, state)
                    _log_line(log_path, f"STOP kill_switch_error_streak={state.error_streak}")
                    _emit(on_event, {"type": "stop", "info": "kill_switch_error_streak"})
                    return
            else:
                state.next_at = datetime.now()
                save_state(state_path, state)
                kill_streak = getattr(settings, "kill_switch_error_streak", None)
                if kill_streak and state.error_streak >= int(kill_streak):
                    state.stopped = True
                    state.stopped_at = datetime.now()
                    save_state(state_path, state)
                    _log_line(log_path, f"STOP kill_switch_error_streak={state.error_streak}")
                    _emit(on_event, {"type": "stop", "info": "kill_switch_error_streak"})
                    return
                continue

        _schedule_next_send(
            state,
            rng,
            campaign,
            settings=settings,
            rate_multiplier=account_rate_multiplier,
            extra_multiplier=extra_mult,
            extra_delay_sec=extra_delay_sec,
        )
        _finish_cycle_if_needed(
            state=state,
            batch_size=batch_size,
            log_path=log_path,
        )
        if use_latest and state.sent_in_current_batch == 0 and state.current_source_id is not None:
            state.last_source_message_ids = state.last_source_message_ids or {}
            state.last_source_message_ids[state.current_source_id] = int(state.current_source_msg_id or 0)
            state.current_source_id = None
            state.current_source_msg_id = None

        next_in = max(0, int((state.next_at - datetime.now()).total_seconds())) if state.next_at else None
        _emit(
            on_event,
            {
                "type": "next",
                "target": last_target_label,
                "link": last_link,
                "info": None,
                "sent_total": state.sent_total,
                "sent_in_batch": state.sent_in_current_batch,
                "next_at": state.next_at,
                "next_in_sec": next_in,
            },
        )

        save_state(state_path, state)
