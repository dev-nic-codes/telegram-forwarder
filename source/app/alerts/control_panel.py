from __future__ import annotations

import html
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.analytics.history_tracker import get_history
from app.core.campaigns import (
    Campaign,
    delete_campaign,
    get_campaign,
    list_campaigns,
    new_campaign_id,
    replace_campaign,
    save_campaign,
)
from app.core.group_sync import (
    load_excluded_group_ids,
    load_group_sync_status,
    load_group_topic_selection,
)
from app.core.sources import add_source, load_sources, remove_source_by_ref, source_ref_from_dialog
from app.core.targets import DestinationTarget, load_targets
from app.utils.paths import DESTINATIONS_CACHE, PROFILES_DIR
from app.utils.storage import load_json
from app.utils.settings import AdvancedSettings, load_settings, save_settings
from app.utils.state import load_state, pause_state, resume_state


def _format_ts(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S %d/%m/%Y")
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).strftime("%H:%M:%S %d/%m/%Y")
        except Exception:
            return value[:19]
    return "-"


def _friendly_error_type(value: object) -> str:
    name = str(value or "Error").strip()
    mapping = {
        "ChatWriteForbiddenError": "Cannot send to this destination",
        "ChatAdminRequiredError": "Sending permission was removed",
        "UserBannedInChannelError": "The account was removed or blocked",
        "ChannelPrivateError": "Destination is no longer accessible",
        "ChatSendMediaForbiddenError": "Media is not allowed",
        "ChatSendPhotosForbiddenError": "Photos are not allowed",
        "ChatSendVideosForbiddenError": "Videos are not allowed",
        "MessageIdInvalidError": "Source message is unavailable",
        "ChatForwardsRestrictedError": "Source content is protected",
        "SlowModeWaitError": "Slow mode is active",
        "FloodWaitError": "Telegram rate limit",
        "ConnectionError": "Telegram connection interrupted",
        "TimeoutError": "Telegram request timed out",
        "OperationalError": "Local database was temporarily busy",
    }
    return mapping.get(name, name.replace("Error", "").replace("_", " ").strip() or "Delivery failed")


def _friendly_exception(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    label = _friendly_error_type(type(exc).__name__)
    return f"{label}. Please try again."


def _safe_parse_next_at(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def _estimate_send_rates(campaign: Campaign) -> tuple[float, float]:
    send_gap = (int(campaign.send_gap_min_sec) + int(campaign.send_gap_max_sec)) / 2.0
    if send_gap <= 0:
        return 0.0, 0.0
    per_hour = 3600.0 / send_gap
    return per_hour, per_hour * 24.0


def _format_duration(seconds: int | float) -> str:
    total_minutes = max(1, int(round(float(seconds) / 60.0)))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _format_interval(min_seconds: int, max_seconds: int) -> str:
    minimum = _format_duration(min_seconds)
    maximum = _format_duration(max_seconds)
    return minimum if minimum == maximum else f"{minimum} - {maximum}"


def _parse_duration(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mh])\s*", value, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("Use minutes or hours, for example 30m, 2h, or 1.5h.")
    amount = float(match.group(1))
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")
    multiplier = 60 if match.group(2).casefold() == "m" else 3600
    return max(60, int(round(amount * multiplier)))


def _parse_duration_range(value: str) -> tuple[int, int]:
    parts = [part.strip() for part in value.split("-", 1)]
    minimum = _parse_duration(parts[0])
    maximum = _parse_duration(parts[1]) if len(parts) == 2 else minimum
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    return minimum, maximum


def _campaign_status(campaign: Campaign) -> tuple[str, str]:
    state = load_state(PROFILES_DIR / f"state_{campaign.id}.json", campaign.id)
    if not getattr(campaign, "enabled", True):
        return "🔴", "Disabled"
    if state is None:
        return "⚪", "Idle"
    if getattr(state, "stopped", False):
        return "⏹️", "Stopped"
    if getattr(state, "paused", False):
        return "⏸️", "Paused"
    return "🟢", "Running"


def _format_windows(windows: list[dict[str, str]] | None) -> str:
    if not windows:
        return "always"
    parts: list[str] = []
    for window in windows:
        start = str(window.get("start") or "").strip()
        end = str(window.get("end") or "").strip()
        if start and end:
            parts.append(f"{start}-{end}")
    return ", ".join(parts) if parts else "always"


def _parse_windows(value: str) -> list[dict[str, str]] | None:
    raw = (value or "").strip()
    if not raw or raw.casefold() in {"none", "clear", "always"}:
        return None
    windows: list[dict[str, str]] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "-" not in item:
            raise ValueError("Use HH:MM-HH:MM,HH:MM-HH:MM")
        start, end = [part.strip() for part in item.split("-", 1)]
        if len(start) != 5 or len(end) != 5 or ":" not in start or ":" not in end:
            raise ValueError("Use HH:MM-HH:MM,HH:MM-HH:MM")
        for point in (start, end):
            hour_text, minute_text = point.split(":", 1)
            if not hour_text.isdigit() or not minute_text.isdigit():
                raise ValueError("Use valid 24-hour times such as 09:00-17:30.")
            hour = int(hour_text)
            minute = int(minute_text)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Use valid 24-hour times such as 09:00-17:30.")
        windows.append({"start": start, "end": end})
    return windows or None


def _parse_latest_sources(value: str) -> list[str]:
    aliases = {
        "saved": "me",
        "saved messages": "me",
        "saved message": "me",
        "savedmessages": "me",
        "me": "me",
        "self": "me",
    }
    lines: list[str] = []
    for raw_line in (value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = aliases.get(line.casefold(), line)
        lines.append(normalized)
    if not lines:
        raise ValueError("Send one or more source links, one per line.")
    return lines


def _parse_message_links(value: str) -> list[str]:
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("Send one or more Telegram message links, one per line.")
    normalized: list[str] = []
    for line in lines:
        clean = line.split("?", 1)[0].rstrip("/")
        if re.fullmatch(r"https?://t\.me/(?:c/\d+|[A-Za-z0-9_]{4,})(?:/\d+)+", clean) is None:
            raise ValueError(f"Not a Telegram message link: {line}")
        normalized.append(clean)
    return normalized


def _format_source_refs(sources: list[str] | None, *, limit: int = 8) -> str:
    items = [str(item).strip() for item in (sources or []) if str(item).strip()]
    if not items:
        return "-"
    labels: list[str] = []
    for raw in items[:limit]:
        if raw.casefold() == "me":
            labels.append("Saved Messages")
        else:
            labels.append(raw)
    if len(items) > limit:
        labels.append(f"...and {len(items) - limit} more")
    return "\n".join(f"• <code>{html.escape(label)}</code>" for label in labels)


def _clone_windows(windows: list[dict[str, str]] | None) -> list[dict[str, str]] | None:
    if not windows:
        return None
    return [dict(item) for item in windows if isinstance(item, dict)]


def _parse_selection(raw: str, *, max_index: int) -> list[int]:
    value = (raw or "").strip().casefold()
    if not value:
        return []
    if value == "all":
        return list(range(1, max_index + 1))

    selected: set[int] = set()
    for chunk in value.split(","):
        item = chunk.strip()
        if not item:
            continue
        if "-" in item:
            left, right = [part.strip() for part in item.split("-", 1)]
            start = int(left)
            end = int(right)
            if start > end:
                start, end = end, start
            for number in range(start, end + 1):
                if 1 <= number <= max_index:
                    selected.add(number)
            continue
        number = int(item)
        if 1 <= number <= max_index:
            selected.add(number)
    return sorted(selected)


def _target_key(target: DestinationTarget | dict[str, Any]) -> tuple[int, int | None]:
    if isinstance(target, DestinationTarget):
        return target.key()
    group_id = int(target.get("group_id") or 0)
    topic_id_raw = target.get("topic_id")
    topic_id = int(topic_id_raw) if topic_id_raw is not None else None
    return group_id, topic_id


def _target_dict(target: DestinationTarget | dict[str, Any]) -> dict[str, Any]:
    if isinstance(target, dict):
        return dict(target)
    return {
        "group_id": int(target.group_id),
        "group_title": str(target.group_title),
        "peer_type": str(getattr(target, "peer_type", "channel") or "channel"),
        "topic_id": int(target.topic_id) if target.topic_id is not None else None,
        "topic_title": str(target.topic_title) if target.topic_title is not None else None,
        "topic_top_message": int(target.topic_top_message) if target.topic_top_message is not None else None,
        "paid_message_stars": int(target.paid_message_stars) if target.paid_message_stars is not None else None,
        "is_paid": bool(target.is_paid),
        "extra_delay_sec": int(target.extra_delay_sec) if target.extra_delay_sec is not None else None,
    }


def _target_label(target: DestinationTarget | dict[str, Any]) -> str:
    if isinstance(target, DestinationTarget):
        group_title = target.group_title
        topic_title = target.topic_title
        is_paid = bool(target.is_paid)
        extra_delay = target.extra_delay_sec
    else:
        group_title = str(target.get("group_title") or "Unknown")
        topic_title = str(target.get("topic_title") or "").strip() or None
        is_paid = bool(target.get("is_paid"))
        extra_delay = target.get("extra_delay_sec")
    label = group_title
    if topic_title:
        label += f" / {topic_title}"
    if is_paid:
        label += " • paid"
    if extra_delay:
        label += f" • +{_format_duration(int(extra_delay))} delay"
    return label


def _limit(value: int, *, low: int = 1, high: int = 50) -> int:
    return max(low, min(high, int(value)))


def _saved_sources_lines(*, limit: int = 30) -> list[str]:
    sources = load_sources()
    lines = [
        f"{index}. {html.escape(source.label)} · <code>{html.escape(source.ref)}</code>"
        for index, source in enumerate(sources[:limit], start=1)
    ]
    if len(sources) > limit:
        lines.append(f"...and {len(sources) - limit} more saved sources.")
    return lines


def _parse_source_input(raw: str) -> list[str]:
    sources = load_sources()
    value = (raw or "").strip()
    if sources and (value.casefold() == "all" or re.fullmatch(r"[\d,\-\s]+", value)):
        indexes = _parse_selection(value, max_index=len(sources))
        if not indexes:
            raise ValueError("Select at least one saved source.")
        return [sources[index - 1].ref for index in indexes]
    return _parse_latest_sources(raw)


_CAMPAIGN_EDIT_PARSERS = {
    "name": "text",
    "message_links": "links",
    "latest_sources": "links",
    "message_interval": "duration_range",
    "schedule_windows": "windows",
    "schedule_windows_weekday": "windows",
    "schedule_windows_weekend": "windows",
    "sleep_start": "time",
    "sleep_end": "time",
    "daily_cap": "optional_int",
    "max_msgs_per_hour": "optional_int",
    "per_target_daily_cap": "optional_int",
    "per_target_cooldown_sec": "optional_duration",
    "bot_alert_every_n": "optional_int",
    "warmup_minutes": "optional_int",
    "warmup_start_multiplier": "float",
    "warmup_end_multiplier": "float",
}
_CAMPAIGN_TOGGLE_FIELDS = {
    "use_latest_source",
    "adaptive_backoff_enabled",
    "warmup_enabled",
}
_CAMPAIGN_CYCLE_FIELDS = {
    "schedule_days",
    "latest_source_strategy",
    "message_strategy",
    "target_strategy",
    "bot_alert_mode",
}
_CAMPAIGN_FIELD_ALIASES = {
    "pt_cooldown": "per_target_cooldown_sec",
}


def _campaign_field_name(value: str) -> str:
    return _CAMPAIGN_FIELD_ALIASES.get(value, value)


@dataclass
class PendingInput:
    scope: str
    field: str
    parser: str
    campaign_id: str | None
    return_to: str
    prompt: str
    extra: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class InputResult:
    message: str
    next_pending: PendingInput | None = None


class ForwarderInlineControlPanel:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._pending_inputs: dict[int, PendingInput] = {}

    async def send_home(self, chat_id: int) -> None:
        if not self.manager.bot:
            return
        await self.manager.bot.send_message(
            chat_id=chat_id,
            text=await self._home_text(),
            reply_markup=self._home_keyboard(),
            parse_mode="HTML",
        )

    async def try_handle_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        if not update.message or not update.message.text or not update.effective_user:
            return False
        pending = self._pending_inputs.get(update.effective_user.id)
        if pending is None:
            return False

        raw = (update.message.text or "").strip()
        if raw.casefold() in {"cancel", "/cancel"}:
            self._pending_inputs.pop(update.effective_user.id, None)
            await update.message.reply_text("🚫 Edit cancelled.")
            return True

        try:
            if pending.scope == "campaign":
                result = InputResult(self._apply_campaign_input(pending, raw))
                result.message += "\n\n" + html.escape(
                    await self._reload_campaign_after_change(str(pending.campaign_id))
                )
            elif pending.scope == "settings":
                result = InputResult(self._apply_settings_input(pending, raw))
            elif pending.scope == "compose":
                result = self._apply_compose_input(pending, raw)
            elif pending.scope == "targets":
                result = self._apply_target_input(pending, raw)
                result.message += "\n\n" + html.escape(
                    await self._reload_campaign_after_change(str(pending.campaign_id))
                )
            elif pending.scope == "source":
                result = await self._apply_source_input(pending, raw)
            else:
                raise ValueError(f"Unknown pending scope: {pending.scope}")
        except Exception as exc:
            await update.message.reply_text(
                f"⚠️ Could not save that value.\n\n{html.escape(str(exc))}\n\n{pending.prompt}",
                parse_mode="HTML",
            )
            return True

        if result.next_pending is not None:
            self._pending_inputs[update.effective_user.id] = result.next_pending
            await update.message.reply_text(result.message, parse_mode="HTML")
            return True

        self._pending_inputs.pop(update.effective_user.id, None)
        await update.message.reply_text(result.message, parse_mode="HTML")
        return True

    async def on_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if query is None or query.message is None or not query.data:
            return

        data = str(query.data)
        parts = data.split(":")
        try:
            if data == "fw:home":
                await query.answer()
                await self._edit(query, await self._home_text(), self._home_keyboard())
                return
            if data == "fw:running":
                await query.answer()
                text = await self._control_text("list_running", "Running ads unavailable.")
                await self._edit(query, text, self._back_keyboard("fw:home", "🏠 Main Menu"))
                return
            if data == "fw:health":
                await query.answer()
                text = await self._control_text("health", "Health unavailable.")
                await self._edit(query, text, self._back_keyboard("fw:home", "🏠 Main Menu"))
                return
            if data == "fw:stats":
                await query.answer()
                await self._edit(query, await self._stats_text(), self._back_keyboard("fw:home", "🏠 Main Menu"))
                return
            if data == "fw:recent":
                await query.answer()
                await self._edit(query, await self._recent_text(), self._back_keyboard("fw:settings:section:activity", "⬅️ Activity & Logs"))
                return
            if data == "fw:errors":
                await query.answer()
                await self._edit(query, await self._errors_text(), self._back_keyboard("fw:settings:section:activity", "⬅️ Activity & Logs"))
                return
            if data == "fw:logs":
                await query.answer()
                await self._edit(query, await self._logs_text(), self._back_keyboard("fw:settings:section:activity", "⬅️ Activity & Logs"))
                return
            if data == "fw:activity":
                await query.answer()
                await self._edit(
                    query,
                    self._settings_section_text(load_settings(), "activity"),
                    self._settings_section_keyboard("activity"),
                )
                return
            if data == "fw:help":
                await query.answer()
                await self._edit(query, self._help_text(), self._back_keyboard("fw:home", "🏠 Main Menu"))
                return
            if data == "fw:resources":
                await query.answer()
                await self._edit(query, self._resources_text(), self._resources_keyboard())
                return
            if data == "fw:resources:scan":
                await query.answer("Scanning Telegram groups...")
                control = self.manager.control
                callback = getattr(control, "scan_groups", None) if control else None
                result = await callback() if callback else "Group scanning is unavailable."
                await self._edit(
                    query,
                    f"{self._resources_text()}\n\n{html.escape(result)}",
                    self._resources_keyboard(),
                )
                return
            if len(parts) >= 4 and parts[1:3] == ["resources", "groups"]:
                page = max(0, int(parts[3]))
                await query.answer()
                await self._edit(query, self._groups_text(page), self._groups_keyboard(page))
                return
            if len(parts) >= 6 and parts[1:3] == ["resources", "group"] and parts[3] == "toggle":
                group_id = int(parts[4])
                page = max(0, int(parts[5]))
                enable = group_id in load_excluded_group_ids()
                control = self.manager.control
                callback = getattr(control, "set_group_enabled", None) if control else None
                await query.answer("Updating group selection...")
                result = await callback(group_id, enable) if callback else "Group selection is unavailable."
                await self._edit(
                    query,
                    f"{self._groups_text(page)}\n\n✅ {html.escape(result)}",
                    self._groups_keyboard(page),
                )
                return
            if len(parts) == 6 and parts[1:3] == ["resources", "topics"]:
                group_id = int(parts[3])
                group_page = max(0, int(parts[4]))
                topic_page = max(0, int(parts[5]))
                await query.answer()
                await self._edit(
                    query,
                    self._topics_text(group_id, topic_page),
                    self._topics_keyboard(group_id, group_page, topic_page),
                )
                return
            if len(parts) == 7 and parts[1:3] == ["resources", "topic"]:
                group_id = int(parts[3])
                topic_id = int(parts[4])
                group_page = max(0, int(parts[5]))
                topic_page = max(0, int(parts[6]))
                selected = load_group_topic_selection().get(group_id, set())
                enable = topic_id not in selected
                control = self.manager.control
                callback = getattr(control, "set_group_topic_enabled", None) if control else None
                await query.answer("Updating topic selection...")
                result = (
                    await callback(group_id, topic_id, enable)
                    if callback
                    else "Topic selection is unavailable."
                )
                await self._edit(
                    query,
                    f"{self._topics_text(group_id, topic_page)}\n\n✅ {html.escape(result)}",
                    self._topics_keyboard(group_id, group_page, topic_page),
                )
                return
            if len(parts) >= 4 and parts[1:3] == ["resources", "sources"]:
                page = max(0, int(parts[3]))
                await query.answer()
                await self._edit(query, self._sources_text(page), self._sources_keyboard(page))
                return
            if len(parts) == 4 and parts[1:3] == ["resources", "sourcegroups"]:
                page = max(0, int(parts[3]))
                await query.answer()
                await self._edit(query, self._source_groups_text(page), self._source_groups_keyboard(page))
                return
            if len(parts) == 6 and parts[1:4] == ["resources", "sourcegroup", "toggle"]:
                group_id = int(parts[4])
                page = max(0, int(parts[5]))
                group = self._cached_group(group_id)
                if not group or str(group.get("kind") or "") != "group":
                    await query.answer("This group is no longer available. Run Scan Groups.", show_alert=True)
                    await self._edit(query, self._source_groups_text(page), self._source_groups_keyboard(page))
                    return
                source_ref = source_ref_from_dialog(group_id, str(group.get("peer_type") or "channel"))
                existing_refs = {source.ref.casefold() for source in load_sources()}
                title = str(group.get("title") or source_ref)
                if source_ref.casefold() in existing_refs:
                    remove_source_by_ref(source_ref)
                    result = f"Removed {title} from saved sources."
                else:
                    add_source(ref=source_ref, label=title, kind="group")
                    result = f"Added {title} as a private-group source."
                await query.answer(result, show_alert=True)
                await self._edit(query, self._source_groups_text(page), self._source_groups_keyboard(page))
                return
            if data == "fw:resources:source:add":
                prompt = (
                    "➕ <b>Add Source</b>\n\n"
                    "Send one or more Telegram source chats, one per line.\n\n"
                    "Accepted formats:\n"
                    "• <code>@channel</code>\n"
                    "• <code>https://t.me/channel</code>\n"
                    "• a private group or topic <code>https://t.me/c/...</code> link\n"
                    "• <code>saved</code> for Saved Messages"
                )
                self._pending_inputs[query.from_user.id] = PendingInput(
                    scope="source",
                    field="add",
                    parser="links",
                    campaign_id=None,
                    return_to="resources",
                    prompt=prompt,
                )
                await query.answer("Waiting for source.")
                await query.message.reply_text(prompt, parse_mode="HTML")
                return
            if len(parts) >= 5 and parts[1:4] == ["resources", "source", "remove"]:
                index = int(parts[4])
                control = self.manager.control
                callback = getattr(control, "remove_source", None) if control else None
                result = await callback(index) if callback else "Source removal is unavailable."
                await query.answer(result, show_alert=True)
                await self._edit(query, self._sources_text(0), self._sources_keyboard(0))
                return
            if data == "fw:settings":
                await query.answer()
                await self._edit(query, self._settings_text(load_settings()), self._settings_keyboard())
                return
            if len(parts) >= 4 and parts[1:3] == ["settings", "section"]:
                section = parts[3]
                await query.answer()
                await self._edit(
                    query,
                    self._settings_section_text(load_settings(), section),
                    self._settings_section_keyboard(section),
                )
                return
            if len(parts) >= 3 and parts[1] == "settings" and parts[2] == "toggle":
                field = parts[3]
                settings = load_settings()
                setattr(settings, field, not bool(getattr(settings, field, False)))
                save_settings(settings)
                control = self.manager.control
                if control is not None and getattr(control, "reload_settings", None) is not None:
                    control.reload_settings()
                await query.answer("Saved.")
                section = self._settings_section_for_field(field)
                await self._edit(
                    query,
                    self._settings_section_text(settings, section),
                    self._settings_section_keyboard(section),
                )
                return
            if len(parts) >= 3 and parts[1] == "settings" and parts[2] == "edit":
                field = parts[3]
                parser = parts[4]
                prompt = self._settings_prompt(field, parser)
                self._pending_inputs[query.from_user.id] = PendingInput(
                    scope="settings",
                    field=field,
                    parser=parser,
                    campaign_id=None,
                    return_to="settings",
                    prompt=prompt,
                )
                await query.answer("Waiting for input.")
                await query.message.reply_text(prompt, parse_mode="HTML")
                return
            if data == "fw:ads:new":
                settings = load_settings()
                default_interval = _format_interval(
                    settings.default_send_gap_min_sec,
                    settings.default_send_gap_max_sec,
                )
                prompt = (
                    "➕ <b>Create New Ad</b>\n\n"
                    "Send the ad name.\n\n"
                    f"Default message interval: <code>{default_interval}</code>."
                )
                self._pending_inputs[query.from_user.id] = PendingInput(
                    scope="compose",
                    field="name",
                    parser="text",
                    campaign_id=None,
                    return_to="ads",
                    prompt=prompt,
                )
                await query.answer("Waiting for ad name.")
                await query.message.reply_text(prompt, parse_mode="HTML")
                return
            if len(parts) >= 2 and parts[1] == "ads":
                page = int(parts[2]) if len(parts) > 2 else 0
                await query.answer()
                await self._edit(query, self._ads_text(page), self._ads_keyboard(page))
                return
            if len(parts) >= 3 and parts[1] == "ad":
                campaign_id = parts[2]
                await query.answer()
                await self._edit(query, self._campaign_text(campaign_id), self._campaign_keyboard(campaign_id))
                return
            if len(parts) >= 4 and parts[1] == "logs" and parts[2] == "ad":
                campaign_id = parts[3]
                await query.answer()
                await self._edit(query, await self._campaign_logs_text(campaign_id), self._back_keyboard(f"fw:ad:{campaign_id}", "⬅️ Back To Ad"))
                return
            if len(parts) >= 4 and parts[1] == "stats" and parts[2] == "ad":
                campaign_id = parts[3]
                await query.answer()
                await self._edit(
                    query,
                    await self._campaign_stats_text(campaign_id),
                    self._back_keyboard(f"fw:ad:{campaign_id}", "⬅️ Back To Ad"),
                )
                return
            if len(parts) >= 4 and parts[1] == "report" and parts[2] == "ad":
                campaign_id = parts[3]
                await query.answer()
                await self._edit(
                    query,
                    await self._campaign_client_report_text(campaign_id),
                    self._back_keyboard(f"fw:ad:{campaign_id}", "⬅️ Back To Ad"),
                )
                return
            if len(parts) >= 4 and parts[1] == "section":
                campaign_id = parts[2]
                section = parts[3]
                await query.answer()
                await self._edit(query, self._campaign_section_text(campaign_id, section), self._campaign_section_keyboard(campaign_id, section))
                return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "manage":
                campaign_id = parts[3]
                action = parts[4]
                if action == "rename":
                    prompt = self._campaign_prompt(campaign_id, "name", "text")
                    self._pending_inputs[query.from_user.id] = PendingInput(
                        scope="campaign",
                        field="name",
                        parser="text",
                        campaign_id=campaign_id,
                        return_to="campaign",
                        prompt=prompt,
                    )
                    await query.answer("Waiting for new name.")
                    await query.message.reply_text(prompt, parse_mode="HTML")
                    return
                if action == "messages":
                    prompt = self._campaign_prompt(campaign_id, "message_links", "links")
                    self._pending_inputs[query.from_user.id] = PendingInput(
                        scope="campaign",
                        field="message_links",
                        parser="links",
                        campaign_id=campaign_id,
                        return_to="campaign",
                        prompt=prompt,
                    )
                    await query.answer("Waiting for message links.")
                    await query.message.reply_text(prompt, parse_mode="HTML")
                    return
                if action == "targets":
                    await query.answer()
                    await self._edit(query, self._campaign_targets_text(campaign_id), self._campaign_targets_keyboard(campaign_id))
                    return
                if action == "deleteask":
                    await query.answer()
                    await self._edit(query, self._delete_confirm_text(campaign_id), self._delete_confirm_keyboard(campaign_id))
                    return
                if action == "delete":
                    campaign = self._campaign_or_raise(campaign_id)
                    stop_notice = await self._stop_running_if_needed(campaign_id)
                    deleted = delete_campaign(campaign_id)
                    self._delete_campaign_state(campaign_id)
                    await query.answer("Deleted." if deleted else "Ad missing.", show_alert=False)
                    details = [f"🗑️ Deleted <b>{html.escape(campaign.name)}</b>." if deleted else "⚠️ Ad was not found."]
                    if stop_notice:
                        details.append(stop_notice)
                    details.append("Saved ad removed. State file cleared. History was kept.")
                    await self._edit(
                        query,
                        "\n\n".join(details),
                        self._back_keyboard("fw:ads:0", "⬅️ Back To Ads"),
                    )
                    return
            if len(parts) >= 4 and parts[1] == "campaign" and parts[2] == "targets":
                campaign_id = parts[3]
                await query.answer()
                await self._edit(query, self._campaign_targets_text(campaign_id), self._campaign_targets_keyboard(campaign_id))
                return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "preset":
                campaign_id = parts[3]
                preset = parts[4]
                campaign = self._campaign_or_raise(campaign_id)
                if preset == "saved":
                    campaign.use_latest_source = True
                    campaign.latest_sources = ["me"]
                    replace_campaign(campaign)
                    await query.answer("Applying source change...")
                    notice = await self._reload_campaign_after_change(campaign_id)
                    await self._edit(
                        query,
                        f"{self._campaign_section_text(campaign_id, 'sources')}\n\n{html.escape(notice)}",
                        self._campaign_section_keyboard(campaign_id, "sources"),
                    )
                    return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "target":
                campaign_id = parts[3]
                action = parts[4]
                prompt = self._target_prompt(campaign_id, action)
                self._pending_inputs[query.from_user.id] = PendingInput(
                    scope="targets",
                    field=action,
                    parser="selection",
                    campaign_id=campaign_id,
                    return_to="targets",
                    prompt=prompt,
                )
                await query.answer("Waiting for target selection.")
                await query.message.reply_text(prompt, parse_mode="HTML")
                return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "toggle":
                campaign_id = parts[3]
                field = _campaign_field_name(parts[4])
                if field not in _CAMPAIGN_TOGGLE_FIELDS:
                    raise ValueError("This ad setting cannot be toggled.")
                campaign = self._campaign_or_raise(campaign_id)
                setattr(campaign, field, not bool(getattr(campaign, field, False)))
                if field == "use_latest_source" and getattr(campaign, field) and not list(getattr(campaign, "latest_sources", []) or []):
                    setattr(campaign, field, False)
                    raise ValueError("Add at least one latest-source link before enabling latest-source mode.")
                if field == "use_latest_source" and not getattr(campaign, field) and not list(campaign.message_links or []):
                    setattr(campaign, field, True)
                    raise ValueError("Add at least one fixed message before disabling latest-source mode.")
                replace_campaign(campaign)
                await query.answer("Applying setting...")
                notice = await self._reload_campaign_after_change(campaign_id)
                section = "sources" if field == "use_latest_source" else "modes"
                await self._edit(
                    query,
                    f"{self._campaign_section_text(campaign_id, section)}\n\n{html.escape(notice)}",
                    self._campaign_section_keyboard(campaign_id, section),
                )
                return
            if len(parts) >= 6 and parts[1] == "campaign" and parts[2] == "edit":
                campaign_id = parts[3]
                field = _campaign_field_name(parts[4])
                parser = parts[5]
                expected_parser = _CAMPAIGN_EDIT_PARSERS.get(field)
                if expected_parser is None or parser != expected_parser:
                    raise ValueError("This ad setting is not editable from this button.")
                prompt = self._campaign_prompt(campaign_id, field, parser)
                self._pending_inputs[query.from_user.id] = PendingInput(
                    scope="campaign",
                    field=field,
                    parser=parser,
                    campaign_id=campaign_id,
                    return_to="campaign",
                    prompt=prompt,
                )
                await query.answer("Waiting for input.")
                await query.message.reply_text(prompt, parse_mode="HTML")
                return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "cycle":
                campaign_id = parts[3]
                field = _campaign_field_name(parts[4])
                if field not in _CAMPAIGN_CYCLE_FIELDS:
                    raise ValueError("This ad setting cannot be cycled.")
                campaign = self._campaign_or_raise(campaign_id)
                current = str(getattr(campaign, field) or "all")
                if field == "schedule_days":
                    values = ["all", "weekday", "weekend"]
                elif field == "bot_alert_mode":
                    values = ["default", "every", "summary", "errors"]
                else:
                    values = ["shuffle_bag", "round_robin"]
                index = values.index(current) if current in values else 0
                next_value = values[(index + 1) % len(values)]
                if field == "bot_alert_mode":
                    campaign.bot_alert_mode = None if next_value == "default" else next_value
                    if next_value != "summary":
                        campaign.bot_alert_every_n = None
                else:
                    setattr(campaign, field, next_value)
                replace_campaign(campaign)
                await query.answer("Applying setting...")
                notice = await self._reload_campaign_after_change(campaign_id)
                if field == "schedule_days":
                    section = "schedule"
                elif field == "latest_source_strategy":
                    section = "sources"
                else:
                    section = "modes"
                await self._edit(
                    query,
                    f"{self._campaign_section_text(campaign_id, section)}\n\n{html.escape(notice)}",
                    self._campaign_section_keyboard(campaign_id, section),
                )
                return
            if len(parts) >= 5 and parts[1] == "campaign" and parts[2] == "action":
                campaign_id = parts[3]
                action = parts[4]
                await query.answer("Working...")
                notice = await self._run_campaign_action(campaign_id, action)
                await self._edit(query, f"{self._campaign_text(campaign_id)}\n\n{notice}", self._campaign_keyboard(campaign_id))
                return
        except Exception as exc:
            try:
                await query.answer("Action failed.", show_alert=True)
            except Exception:
                pass
            await query.message.reply_text(
                f"⚠️ <b>Action could not be completed</b>\n\n{html.escape(_friendly_exception(exc))}",
                parse_mode="HTML",
            )

    async def _control_text(self, field: str, fallback: str) -> str:
        control = self.manager.control
        if control is None:
            return fallback
        callback = getattr(control, field, None)
        if callback is None:
            return fallback
        return await callback()


    async def _campaign_logs_text(self, campaign_id: str) -> str:
        history = await get_history()
        records = await history.get_recent(limit=50)
        filtered = [row for row in records if str(row.get("ad_id") or row.get("campaign_id") or "") == campaign_id][:15]
        campaign = self._campaign_or_raise(campaign_id)
        lines = [f"🧾 Recent activity for {campaign.name}"]
        if not filtered:
            lines.append("")
            lines.append("No recent history found for this ad.")
            return "\n".join(lines)
        for row in filtered:
            ts = _format_ts(row.get("timestamp"))
            group = str(row.get("group_title") or "Unknown")
            status = "✅ OK" if row.get("success") else "❌ FAIL"
            lines.append(f"- {ts} | {status} | {group}")
        return "\n".join(lines)

    async def _campaign_stats_text(self, campaign_id: str) -> str:
        history = await get_history()
        records = await history.get_by_campaign(campaign_id)
        campaign = self._campaign_or_raise(campaign_id)
        icon, status = _campaign_status(campaign)
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)

        def in_period(row: dict[str, Any], start: datetime) -> bool:
            timestamp = _safe_parse_next_at(row.get("timestamp"))
            return timestamp is not None and timestamp >= start

        def summary(rows: list[dict[str, Any]]) -> tuple[int, int, int, float]:
            total = len(rows)
            successful = sum(1 for row in rows if row.get("success"))
            failed = total - successful
            success_rate = (successful / total * 100.0) if total else 0.0
            return total, successful, failed, success_rate

        today = [row for row in records if in_period(row, today_start)]
        week = [row for row in records if in_period(row, week_start)]
        today_total, today_ok, today_failed, today_rate = summary(today)
        week_total, week_ok, week_failed, week_rate = summary(week)
        total, successful, failed, success_rate = summary(records)
        destinations = len(
            {
                (
                    int(row.get("group_id") or 0),
                    int(row.get("topic_id")) if row.get("topic_id") is not None else None,
                )
                for row in records
            }
        )
        stars_spent = sum(int(row.get("stars_cost") or 0) for row in records)
        first_sent = _format_ts(records[-1].get("timestamp")) if records else "-"
        last_sent = _format_ts(records[0].get("timestamp")) if records else "-"
        return (
            f"📊 <b>Ad Statistics</b>\n\n"
            f"<b>Ad:</b> {html.escape(campaign.name)}\n"
            f"<b>Status:</b> {icon} {html.escape(status)}\n\n"
            f"📅 <b>Today</b>\n"
            f"• Sent: <b>{today_total}</b>\n"
            f"• Successful: <b>{today_ok}</b>\n"
            f"• Failed: <b>{today_failed}</b>\n"
            f"• Success rate: <b>{today_rate:.1f}%</b>\n\n"
            f"🗓️ <b>Last 7 Days</b>\n"
            f"• Sent: <b>{week_total}</b>\n"
            f"• Successful: <b>{week_ok}</b>\n"
            f"• Failed: <b>{week_failed}</b>\n"
            f"• Success rate: <b>{week_rate:.1f}%</b>\n\n"
            f"📈 <b>All Time</b>\n"
            f"• Sent: <b>{total}</b>\n"
            f"• Successful: <b>{successful}</b>\n"
            f"• Failed: <b>{failed}</b>\n"
            f"• Success rate: <b>{success_rate:.1f}%</b>\n"
            f"• Destinations reached: <b>{destinations}</b>\n"
            f"• Stars spent: <b>{stars_spent}</b>\n\n"
            f"🕒 <b>Timeline</b>\n"
            f"• First send: {html.escape(first_sent)}\n"
            f"• Last send: {html.escape(last_sent)}"
        )

    async def _campaign_client_report_text(self, campaign_id: str) -> str:
        history = await get_history()
        records = await history.get_by_campaign(campaign_id)
        campaign = self._campaign_or_raise(campaign_id)
        icon, status = _campaign_status(campaign)
        total = len(records)
        successful = sum(1 for row in records if row.get("success"))
        failed = total - successful
        success_rate = (successful / total * 100.0) if total > 0 else 0.0
        unique_groups = len(
            {
                (
                    int(row.get("group_id") or 0),
                    int(row.get("topic_id")) if row.get("topic_id") is not None else None,
                )
                for row in records
            }
        )
        stars_spent = sum(int(row.get("stars_cost") or 0) for row in records)
        first_sent = _format_ts(records[-1].get("timestamp")) if records else "-"
        last_sent = _format_ts(records[0].get("timestamp")) if records else "-"
        source_count = len(campaign.latest_sources or []) if campaign.use_latest_source else len(campaign.message_links or [])
        source_mode = "Latest feed mode" if campaign.use_latest_source else "Fixed message links"
        return (
            f"📣 <b>Client Ad Report</b>\n\n"
            f"<b>Ad:</b> {html.escape(campaign.name)}\n"
            f"<b>ID:</b> <code>{campaign.id}</code>\n"
            f"<b>Status:</b> {icon} {html.escape(status)}\n\n"
            f"📊 <b>Delivery Stats</b>\n"
            f"• <b>Total sends:</b> <code>{total}</code>\n"
            f"• <b>Successful:</b> <code>{successful}</code>\n"
            f"• <b>Failed:</b> <code>{failed}</code>\n"
            f"• <b>Success rate:</b> <code>{success_rate:.1f}%</code>\n"
            f"• <b>Groups reached:</b> <code>{unique_groups}</code>\n"
            f"• <b>Stars spent:</b> <code>{stars_spent}</code>\n\n"
            f"🧩 <b>Ad Setup</b>\n"
            f"• <b>Targets:</b> <code>{len(campaign.target_refs or [])}</code>\n"
            f"• <b>Source mode:</b> <code>{html.escape(source_mode)}</code>\n"
            f"• <b>Source count:</b> <code>{source_count}</code>\n\n"
            f"🕒 <b>Timeline</b>\n"
            f"• <b>First send:</b> <code>{html.escape(first_sent)}</code>\n"
            f"• <b>Last send:</b> <code>{html.escape(last_sent)}</code>\n\n"
            f"Client-safe summary:\n"
            f"<blockquote>Ad <b>{html.escape(campaign.name)}</b> has been delivered <b>{total}</b> times "
            f"across <b>{unique_groups}</b> target chats with a <b>{success_rate:.1f}%</b> success rate.</blockquote>"
        )





    async def _logs_text(self) -> str:
        control = self.manager.control
        if control and getattr(control, "recent_logs", None) is not None:
            return await control.recent_logs(15)
        return "🧾 <b>Recent Logs</b>\n\nNo live log provider is available."

    async def _stats_text(self) -> str:
        history = await get_history()
        totals = await history.get_totals()
        total = int(totals.get("total") or 0)
        successful = int(totals.get("successful") or 0)
        failed = int(totals.get("failed") or 0)
        success_rate = (successful / total * 100.0) if total > 0 else 0.0
        active = len([campaign for campaign in list_campaigns() if getattr(campaign, "enabled", True)])
        return (
            "📈 <b>Forwarder Stats</b>\n\n"
            "📨 <b>Delivery</b>\n"
            f"• <b>Total messages:</b> {total}\n"
            f"• <b>Successful:</b> {successful}\n"
            f"• <b>Failed:</b> {failed}\n"
            f"• <b>Success rate:</b> {success_rate:.1f}%\n\n"
            "🎯 <b>Ads</b>\n"
            f"• <b>Enabled ads:</b> {active}"
        )

    async def _recent_text(self) -> str:
        history = await get_history()
        records = await history.get_recent(limit=15)
        lines = ["📝 <b>Recent Sends</b>", "", "Latest deliveries across all ads."]
        if not records:
            lines.extend(["", "No history found yet."])
            return "\n".join(lines)
        for row in records[:15]:
            ts = _format_ts(row.get("timestamp"))
            ad = str(row.get("campaign_name") or "Unknown")
            group = str(row.get("group_title") or "Unknown")
            status = "✅ OK" if row.get("success") else "❌ FAIL"
            lines.append(f"• <code>{ts}</code> | {status} | <b>{html.escape(ad)}</b> → {html.escape(group)}")
        return "\n".join(lines)

    async def _errors_text(self) -> str:
        history = await get_history()
        records = await history.get_recent_errors(limit=15)
        lines = ["❌ <b>Recent Errors</b>", "", "Latest failed deliveries and their error types."]
        if not records:
            lines.extend(["", "No errors found."])
            return "\n".join(lines)
        for row in records[:15]:
            ts = _format_ts(row.get("timestamp"))
            ad = str(row.get("campaign_name") or "Unknown")
            group = str(row.get("group_title") or "Unknown")
            error_type = _friendly_error_type(row.get("error_type"))
            lines.append(f"• <code>{ts}</code> | <b>{html.escape(ad)}</b> → {html.escape(group)} | ⚠️ {html.escape(error_type)}")
        return "\n".join(lines)

    async def _home_text(self) -> str:
        campaigns = list_campaigns()
        dashboard = await self._dashboard_status()
        running_ids = {str(item) for item in dashboard.get("running_ids", [])}
        lines = [
            "🤖 <b>Forwarder</b>",
            "",
            "📣 <b>Ads</b>",
        ]
        if not campaigns:
            lines.append("No ads have been created yet.")
        for campaign in campaigns:
            state = load_state(PROFILES_DIR / f"state_{campaign.id}.json", campaign.id)
            if campaign.id in running_ids:
                if state is not None and bool(getattr(state, "paused", False)):
                    icon, status = "⏸", "Paused"
                else:
                    icon, status = "✅", "Working"
            elif not bool(getattr(campaign, "enabled", True)):
                icon, status = "⛔", "Disabled"
            else:
                icon, status = "⚠️", "Not running"
            lines.append(f"{icon} <b>{html.escape(campaign.name)}</b> · {status}")

        health_good = bool(dashboard.get("telegram_connected") and dashboard.get("bot_online"))
        health = "✅ Good" if health_good else "⚠️ Needs attention"
        lines.extend(["", f"🩺 <b>System health:</b> {health}"])
        return "\n".join(lines)

    async def _dashboard_status(self) -> dict[str, Any]:
        control = self.manager.control
        callback = getattr(control, "dashboard_status", None) if control else None
        if callback is None:
            return {}
        try:
            value = await callback()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _home_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎛️ Ads", callback_data="fw:ads:0"),
                    InlineKeyboardButton(text="🌐 Groups & Sources", callback_data="fw:resources"),
                ],
                [
                    InlineKeyboardButton(text="📈 Stats", callback_data="fw:stats"),
                    InlineKeyboardButton(text="⚙️ Settings", callback_data="fw:settings"),
                ],
                [
                    InlineKeyboardButton(text="🔄 Refresh", callback_data="fw:home"),
                ],
            ]
        )

    def _resources_text(self) -> str:
        groups = {int(target.group_id) for target in load_targets()}
        available = self._available_groups()
        available_ids = {group_id for group_id, _, _, _ in available}
        excluded = len(available_ids & load_excluded_group_ids())
        paid = len([1 for _, _, stars, _ in available if stars > 0])
        forums = len([1 for _, _, _, is_forum in available if is_forum])
        sources = load_sources()
        status = load_group_sync_status()
        last_scan = _format_ts(status.get("scanned_at")) if status else "Never"
        return (
            "🌐 <b>Groups & Sources</b>\n\n"
            "Choose which groups can receive ads and manage reusable public or private message sources.\n\n"
            f"✅ <b>Selected groups:</b> {len(groups)}\n"
            f"🚫 <b>Excluded groups:</b> {excluded}\n"
            f"⭐ <b>Paid groups:</b> {paid}\n"
            f"🧵 <b>Forum groups:</b> {forums}\n"
            f"📥 <b>Saved sources:</b> {len(sources)}\n"
            f"🕒 <b>Last scan:</b> {html.escape(last_scan)}\n\n"
            "Scheduled scans remember your choices and remove groups the account has left."
        )

    def _resources_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Scan Groups", callback_data="fw:resources:scan")],
                [
                    InlineKeyboardButton(text="✅ Choose Groups", callback_data="fw:resources:groups:0"),
                    InlineKeyboardButton(text="📥 Sources", callback_data="fw:resources:sources:0"),
                ],
                [
                    InlineKeyboardButton(text="🔒 Private Group Sources", callback_data="fw:resources:sourcegroups:0"),
                ],
                [InlineKeyboardButton(text="➕ Add Source Manually", callback_data="fw:resources:source:add")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home")],
            ]
        )

    def _groups_text(self, page: int) -> str:
        groups = self._available_groups()
        excluded = load_excluded_group_ids()
        selected_count = len([group_id for group_id, _, _, _ in groups if group_id not in excluded])
        page_size = 10
        page_count = max(1, (len(groups) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        lines = [
            "✅ <b>Choose Ad Groups</b>",
            "",
            f"Page {page + 1}/{page_count} · {selected_count} of {len(groups)} selected",
            "",
            "Tap a group to include or exclude it from ad destinations.",
            "✅ Included · 🚫 Excluded · ⭐ Paid in Telegram Stars · 🧵 Has topics",
            "Including makes a group available; add it to each ad you want to send there.",
            "",
        ]
        if not groups:
            lines.append("No sendable groups are saved. Run a group scan.")
        for index, group in enumerate(groups[start : start + page_size], start=start + 1):
            icon = "🚫" if group[0] in excluded else "✅"
            paid = f" · ⭐ {group[2]:,} Stars" if group[2] > 0 else ""
            forum = " · 🧵 Topics" if group[3] else ""
            lines.append(f"{index}. {icon} {html.escape(group[1])}{paid}{forum}")
        return "\n".join(lines)

    def _groups_keyboard(self, page: int) -> InlineKeyboardMarkup:
        groups = self._available_groups()
        excluded = load_excluded_group_ids()
        page_size = 10
        page_count = max(1, (len(groups) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        rows: list[list[InlineKeyboardButton]] = []
        start = page * page_size
        for group_id, title, stars, is_forum in groups[start : start + page_size]:
            icon = "🚫" if group_id in excluded else "✅"
            paid = f" ⭐{stars:,}" if stars > 0 else ""
            max_title = max(12, 32 - len(paid))
            label = title if len(title) <= max_title else f"{title[:max_title - 3]}..."
            row = [
                InlineKeyboardButton(
                    text=f"{icon} {label}{paid}",
                    callback_data=f"fw:resources:group:toggle:{group_id}:{page}",
                )
            ]
            if is_forum:
                row.append(
                    InlineKeyboardButton(
                        text="🧵 Topics",
                        callback_data=f"fw:resources:topics:{group_id}:{page}:0",
                    )
                )
            rows.append(row)
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"fw:resources:groups:{page - 1}"))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"fw:resources:groups:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🔄 Scan Groups", callback_data="fw:resources:scan")])
        rows.append([InlineKeyboardButton(text="⬅️ Groups & Sources", callback_data="fw:resources")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _available_groups(self) -> list[tuple[int, str, int, bool]]:
        cached = load_json(DESTINATIONS_CACHE, default=[])
        groups: dict[int, tuple[str, int, bool]] = {}
        if isinstance(cached, list):
            for item in cached:
                if not isinstance(item, dict) or item.get("kind") != "group" or not item.get("sendable"):
                    continue
                try:
                    group_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                try:
                    stars = max(0, int(item.get("paid_message_stars") or 0))
                except (TypeError, ValueError):
                    stars = 0
                groups[group_id] = (
                    str(item.get("title") or group_id),
                    stars,
                    bool(item.get("is_forum")),
                )
        return sorted(
            [
                (group_id, title, stars, is_forum)
                for group_id, (title, stars, is_forum) in groups.items()
            ],
            key=lambda item: item[1].casefold(),
        )

    def _cached_group(self, group_id: int) -> dict:
        cached = load_json(DESTINATIONS_CACHE, default=[])
        if not isinstance(cached, list):
            return {}
        for item in cached:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("id")) == int(group_id):
                    return item
            except (TypeError, ValueError):
                continue
        return {}

    def _topics_text(self, group_id: int, page: int) -> str:
        group = self._cached_group(group_id)
        title = str(group.get("title") or group_id)
        topics = group.get("topics", []) if isinstance(group, dict) else []
        topics = [topic for topic in topics if isinstance(topic, dict)]
        topics.sort(key=lambda topic: str(topic.get("title") or "").casefold())
        selection = load_group_topic_selection()
        selected = selection.get(int(group_id), set())
        page_size = 8
        page_count = max(1, (len(topics) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        lines = [
            "🧵 <b>Choose Forum Topics</b>",
            "",
            f"<b>Group:</b> {html.escape(title)}",
            f"<b>Page:</b> {page + 1}/{page_count}",
            f"<b>Selected:</b> {len(selected)}",
            "",
            "Select the exact topics that new ads may use.",
            "Existing ads keep their current destinations until you edit them.",
            "",
        ]
        if not topics:
            lines.append("No topics are cached. Return to Groups & Sources and run Scan Groups.")
        for index, topic in enumerate(topics[start : start + page_size], start=start + 1):
            try:
                topic_id = int(topic.get("topic_id"))
            except (TypeError, ValueError):
                continue
            icon = "✅" if topic_id in selected else "🚫"
            topic_title = str(topic.get("title") or f"Topic {topic_id}")
            lines.append(f"{index}. {icon} {html.escape(topic_title)}")
        return "\n".join(lines)

    def _topics_keyboard(self, group_id: int, group_page: int, topic_page: int) -> InlineKeyboardMarkup:
        group = self._cached_group(group_id)
        topics = group.get("topics", []) if isinstance(group, dict) else []
        topics = [topic for topic in topics if isinstance(topic, dict)]
        topics.sort(key=lambda topic: str(topic.get("title") or "").casefold())
        selected = load_group_topic_selection().get(int(group_id), set())
        page_size = 8
        page_count = max(1, (len(topics) + page_size - 1) // page_size)
        topic_page = min(max(0, topic_page), page_count - 1)
        start = topic_page * page_size
        rows: list[list[InlineKeyboardButton]] = []
        for topic in topics[start : start + page_size]:
            try:
                topic_id = int(topic.get("topic_id"))
            except (TypeError, ValueError):
                continue
            title = str(topic.get("title") or f"Topic {topic_id}")
            label = title if len(title) <= 34 else f"{title[:31]}..."
            icon = "✅" if topic_id in selected else "🚫"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {label}",
                        callback_data=(
                            f"fw:resources:topic:{group_id}:{topic_id}:{group_page}:{topic_page}"
                        ),
                    )
                ]
            )
        nav: list[InlineKeyboardButton] = []
        if topic_page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="⬅️ Previous",
                    callback_data=f"fw:resources:topics:{group_id}:{group_page}:{topic_page - 1}",
                )
            )
        if topic_page + 1 < page_count:
            nav.append(
                InlineKeyboardButton(
                    text="Next ➡️",
                    callback_data=f"fw:resources:topics:{group_id}:{group_page}:{topic_page + 1}",
                )
            )
        if nav:
            rows.append(nav)
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Choose Groups",
                    callback_data=f"fw:resources:groups:{group_page}",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _activity_text(self) -> str:
        return (
            "📊 <b>Activity</b>\n\n"
            "Review successful sends, delivery errors, and live service logs."
        )

    def _activity_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📝 Recent Sends", callback_data="fw:recent"),
                    InlineKeyboardButton(text="❌ Errors", callback_data="fw:errors"),
                ],
                [InlineKeyboardButton(text="🧾 Runtime Logs", callback_data="fw:logs")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home")],
            ]
        )

    def _source_groups(self) -> list[tuple[int, str, str, str, bool]]:
        cached = load_json(DESTINATIONS_CACHE, default=[])
        saved_refs = {source.ref.casefold() for source in load_sources()}
        groups: list[tuple[int, str, str, str, bool]] = []
        if not isinstance(cached, list):
            return groups
        for item in cached:
            if not isinstance(item, dict) or str(item.get("kind") or "") != "group":
                continue
            try:
                group_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            peer_type = str(item.get("peer_type") or "channel")
            source_ref = source_ref_from_dialog(group_id, peer_type)
            groups.append(
                (
                    group_id,
                    str(item.get("title") or source_ref),
                    peer_type,
                    source_ref,
                    source_ref.casefold() in saved_refs,
                )
            )
        return sorted(groups, key=lambda item: item[1].casefold())

    def _source_groups_text(self, page: int) -> str:
        groups = self._source_groups()
        page_size = 10
        page_count = max(1, (len(groups) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        selected_count = sum(1 for item in groups if item[4])
        lines = [
            "🔒 <b>Private Group Sources</b>",
            "",
            f"Page <code>{page + 1}/{page_count}</code> · Saved <code>{selected_count}/{len(groups)}</code>",
            "",
            "Select any group the logged-in account can read. Public usernames are not required.",
            "Read-only private groups can be sources even when they cannot receive ads.",
            "✅ Saved source · ➕ Available source",
            "",
        ]
        if not groups:
            lines.append("No groups are cached. Return to Groups & Sources and run Scan Groups.")
        for index, (_, title, _, source_ref, selected) in enumerate(
            groups[start : start + page_size],
            start=start + 1,
        ):
            icon = "✅" if selected else "➕"
            lines.append(f"{index}. {icon} <b>{html.escape(title)}</b> · <code>{html.escape(source_ref)}</code>")
        return "\n".join(lines)

    def _source_groups_keyboard(self, page: int) -> InlineKeyboardMarkup:
        groups = self._source_groups()
        page_size = 10
        page_count = max(1, (len(groups) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        rows: list[list[InlineKeyboardButton]] = []
        for group_id, title, _, _, selected in groups[start : start + page_size]:
            label = title if len(title) <= 31 else f"{title[:28]}..."
            icon = "✅" if selected else "➕"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {label}",
                        callback_data=f"fw:resources:sourcegroup:toggle:{group_id}:{page}",
                    )
                ]
            )
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="⬅️ Previous",
                    callback_data=f"fw:resources:sourcegroups:{page - 1}",
                )
            )
        if page + 1 < page_count:
            nav.append(
                InlineKeyboardButton(
                    text="Next ➡️",
                    callback_data=f"fw:resources:sourcegroups:{page + 1}",
                )
            )
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🔄 Scan Groups", callback_data="fw:resources:scan")])
        rows.append([InlineKeyboardButton(text="⬅️ Saved Sources", callback_data="fw:resources:sources:0")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _sources_text(self, page: int) -> str:
        sources = load_sources()
        page_size = 8
        page_count = max(1, (len(sources) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        lines = [
            "📥 <b>Saved Sources</b>",
            "",
            f"Page <code>{page + 1}/{page_count}</code> · Total <code>{len(sources)}</code>",
            "",
        ]
        if not sources:
            lines.append("No sources are saved yet.")
        for index, source in enumerate(sources[start : start + page_size], start=start + 1):
            lines.append(
                f"{index}. <b>{html.escape(source.label)}</b> · <code>{html.escape(source.ref)}</code>"
            )
        lines.extend(["", "Saved sources can be selected by number when creating or editing a latest-source ad."])
        return "\n".join(lines)

    def _sources_keyboard(self, page: int) -> InlineKeyboardMarkup:
        sources = load_sources()
        page_size = 8
        page_count = max(1, (len(sources) + page_size - 1) // page_size)
        page = min(max(0, page), page_count - 1)
        start = page * page_size
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(text="🔒 Select from My Groups", callback_data="fw:resources:sourcegroups:0"),
                InlineKeyboardButton(text="➕ Add Manually", callback_data="fw:resources:source:add"),
            ]
        ]
        for index, source in enumerate(sources[start : start + page_size], start=start + 1):
            label = source.label if len(source.label) <= 28 else f"{source.label[:25]}..."
            rows.append(
                [InlineKeyboardButton(text=f"🗑️ {index}. {label}", callback_data=f"fw:resources:source:remove:{index}")]
            )
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"fw:resources:sources:{page - 1}"))
        if page + 1 < page_count:
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"fw:resources:sources:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="⬅️ Groups & Sources", callback_data="fw:resources")])
        return InlineKeyboardMarkup(inline_keyboard=rows)


    def _ads_text(self, page: int) -> str:
        ads = list_campaigns()
        if not ads:
            return "🎛️ <b>Ads</b>\n\nNo ads found yet."
        start = max(0, page) * 10
        end = start + 10
        lines = ["🎛️ <b>Saved Ads</b>", "", "Open an ad to control sending, message interval, schedule, limits, and delivery options."]
        for campaign in ads[start:end]:
            icon, label = _campaign_status(campaign)
            lines.append(f"• {icon} <b>{html.escape(campaign.name)}</b> <code>({campaign.id})</code> • {label}")
        return "\n".join(lines)






    def _settings_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🕒 Sending", callback_data="fw:settings:section:sending"),
                    InlineKeyboardButton(text="🛡️ Reliability", callback_data="fw:settings:section:reliability"),
                ],
                [
                    InlineKeyboardButton(text="📊 Activity & Logs", callback_data="fw:settings:section:activity"),
                    InlineKeyboardButton(text="🔧 Advanced", callback_data="fw:settings:section:advanced"),
                ],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home")],
            ]
        )


    def _settings_text(self, settings: AdvancedSettings) -> str:
        return (
            "⚙️ <b>Settings</b>\n\n"
            "These are global controls that apply to the whole Forwarder.\n"
            "Timing, destinations, sources, and schedules for one ad are changed from that ad's own menu.\n\n"
            "🕒 <b>Sending</b>\n"
            "Quiet hours, source checking, and defaults for new ads.\n\n"
            "🛡️ <b>Reliability</b>\n"
            "Retries and protection when Telegram rejects a message.\n\n"
            "📊 <b>Activity & Logs</b>\n"
            "Recent sends, understandable errors, and runtime logs.\n\n"
            "🔧 <b>Advanced</b>\n"
            "Account-wide limits and low-level timing controls."
        )

    def _settings_section_for_field(self, field: str) -> str:
        if field in {
            "global_quiet_hours_enabled", "global_quiet_start", "global_quiet_end",
            "latest_source_poll_sec", "default_send_gap_min_sec", "default_send_gap_max_sec",
        }:
            return "sending"
        if field in {
            "continue_on_target_error", "retry_transient_count", "retry_transient_base_delay_sec",
            "cooldown_timeout_sec", "max_error_streak_pause", "auto_disable_target_error_count",
        }:
            return "reliability"
        return "advanced"

    def _settings_section_text(self, settings: AdvancedSettings, section: str) -> str:
        if section == "sending":
            quiet = "On" if settings.global_quiet_hours_enabled else "Off"
            return (
                "🕒 <b>Sending Settings</b>\n\n"
                "Quiet hours stop every ad during the selected period.\n\n"
                f"• <b>Quiet hours:</b> {quiet}\n"
                f"• <b>Quiet period:</b> {html.escape(str(settings.global_quiet_start))}–{html.escape(str(settings.global_quiet_end))}\n"
                f"• <b>Check sources every:</b> {_format_duration(settings.latest_source_poll_sec)}\n"
                f"• <b>New-ad default interval:</b> {_format_interval(settings.default_send_gap_min_sec, settings.default_send_gap_max_sec)}"
            )
        if section == "reliability":
            keep_going = "Yes" if settings.continue_on_target_error else "No"
            pause_after = settings.max_error_streak_pause if settings.max_error_streak_pause else "Disabled"
            return (
                "🛡️ <b>Reliability Settings</b>\n\n"
                "Permanent access or media restrictions remove only the affected destination. Temporary failures are retried.\n\n"
                f"• <b>Continue with other destinations:</b> {keep_going}\n"
                f"• <b>Temporary retries:</b> {settings.retry_transient_count}\n"
                f"• <b>Delay between retries:</b> {settings.retry_transient_base_delay_sec:g}s\n"
                f"• <b>Timeout wait:</b> {_format_duration(settings.cooldown_timeout_sec)}\n"
                f"• <b>Pause after consecutive errors:</b> {pause_after}\n"
                f"• <b>Disable after repeated errors:</b> {settings.auto_disable_target_error_count}\n"
                "• <b>Permanent destination cleanup:</b> On"
            )
        if section == "activity":
            return (
                "📊 <b>Activity & Logs</b>\n\n"
                "Review deliveries, failures, and the latest service activity. Error names are translated into clear descriptions."
            )
        if section == "advanced":
            max_hour = settings.global_max_msgs_per_hour or "No limit"
            return (
                "🔧 <b>Advanced Settings</b>\n\n"
                "Change these only when you understand their effect. Ad-specific intervals still take priority.\n\n"
                f"• <b>Account speed multiplier:</b> {settings.account_rate_multiplier_default:g}×\n"
                f"• <b>Random extra delay:</b> {settings.global_jitter_min_sec}s–{settings.global_jitter_max_sec}s\n"
                f"• <b>Absolute minimum send gap:</b> {settings.global_min_send_gap_sec}s\n"
                f"• <b>Global messages per hour:</b> {max_hour}\n"
                f"• <b>Menu status refresh:</b> {settings.live_update_every_sec}s"
            )
        raise ValueError("Unknown settings section.")

    def _settings_section_keyboard(self, section: str) -> InlineKeyboardMarkup:
        if section == "sending":
            rows = [
                [InlineKeyboardButton(text="🌙 Toggle Quiet Hours", callback_data="fw:settings:toggle:global_quiet_hours_enabled")],
                [
                    InlineKeyboardButton(text="Start Time", callback_data="fw:settings:edit:global_quiet_start:time"),
                    InlineKeyboardButton(text="End Time", callback_data="fw:settings:edit:global_quiet_end:time"),
                ],
                [InlineKeyboardButton(text="🔄 Source Check Interval", callback_data="fw:settings:edit:latest_source_poll_sec:duration")],
                [
                    InlineKeyboardButton(text="Default Minimum", callback_data="fw:settings:edit:default_send_gap_min_sec:duration"),
                    InlineKeyboardButton(text="Default Maximum", callback_data="fw:settings:edit:default_send_gap_max_sec:duration"),
                ],
            ]
        elif section == "reliability":
            rows = [
                [InlineKeyboardButton(text="Continue Other Destinations", callback_data="fw:settings:toggle:continue_on_target_error")],
                [
                    InlineKeyboardButton(text="Temporary Retries", callback_data="fw:settings:edit:retry_transient_count:int"),
                    InlineKeyboardButton(text="Retry Delay", callback_data="fw:settings:edit:retry_transient_base_delay_sec:float"),
                ],
                [
                    InlineKeyboardButton(text="Timeout Wait", callback_data="fw:settings:edit:cooldown_timeout_sec:duration"),
                    InlineKeyboardButton(text="Pause Threshold", callback_data="fw:settings:edit:max_error_streak_pause:optional_int"),
                ],
                [InlineKeyboardButton(text="Disable Threshold", callback_data="fw:settings:edit:auto_disable_target_error_count:int")],
            ]
        elif section == "activity":
            rows = [
                [
                    InlineKeyboardButton(text="📝 Recent Sends", callback_data="fw:recent"),
                    InlineKeyboardButton(text="❌ Errors", callback_data="fw:errors"),
                ],
                [InlineKeyboardButton(text="🧾 Runtime Logs", callback_data="fw:logs")],
            ]
        elif section == "advanced":
            rows = [
                [InlineKeyboardButton(text="Account Speed", callback_data="fw:settings:edit:account_rate_multiplier_default:float")],
                [
                    InlineKeyboardButton(text="Random Delay Min", callback_data="fw:settings:edit:global_jitter_min_sec:int"),
                    InlineKeyboardButton(text="Random Delay Max", callback_data="fw:settings:edit:global_jitter_max_sec:int"),
                ],
                [
                    InlineKeyboardButton(text="Minimum Send Gap", callback_data="fw:settings:edit:global_min_send_gap_sec:int"),
                    InlineKeyboardButton(text="Messages Per Hour", callback_data="fw:settings:edit:global_max_msgs_per_hour:optional_int"),
                ],
                [InlineKeyboardButton(text="Menu Refresh", callback_data="fw:settings:edit:live_update_every_sec:int")],
            ]
        else:
            rows = []
        rows.append([InlineKeyboardButton(text="⬅️ Settings", callback_data="fw:settings")])
        rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _help_text(self) -> str:
        return (
            "❓ <b>Forwarder Help</b>\n\n"
            "Use <b>Ads</b> to create an ad or change its message, timing, schedule, source, and destinations.\n"
            "Use <b>Groups & Sources</b> to choose which groups can receive ads and save reusable message sources.\n"
            "Use <b>Activity</b> to review sends, errors, and runtime logs.\n"
            "Use <b>Settings</b> for global service timing and polling values.\n"
            "Excluded groups stay excluded after automatic scans.\n\n"
            "For text input prompts:\n"
            "• send <code>cancel</code> to abort\n"
            "• send <code>none</code> to clear optional values\n"
            "• schedule windows format: <code>09:00-12:00,14:00-18:00</code>"
        )

    def _back_keyboard(self, callback_data: str, label: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=callback_data)]]
        )

    def _ads_keyboard(self, page: int) -> InlineKeyboardMarkup:
        ads = list_campaigns()
        start = max(0, page) * 10
        end = start + 10
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="➕ New Ad", callback_data="fw:ads:new")]
        ]
        for campaign in ads[start:end]:
            icon, _ = _campaign_status(campaign)
            rows.append([InlineKeyboardButton(text=f"{icon} {campaign.name}", callback_data=f"fw:ad:{campaign.id}")])
        nav: list[InlineKeyboardButton] = []
        if start > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"fw:ads:{page - 1}"))
        if end < len(ads):
            nav.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"fw:ads:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _campaign_text(self, campaign_id: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        state = load_state(PROFILES_DIR / f"state_{campaign.id}.json", campaign.id)
        icon, status = _campaign_status(campaign)
        next_at = _safe_parse_next_at(getattr(state, "next_at", None)) if state else None
        next_text = _format_ts(next_at) if next_at else "-"
        sent_total = getattr(state, "sent_total", 0) if state else 0
        per_hour, per_day = _estimate_send_rates(campaign)
        latest_mode = "on" if getattr(campaign, "use_latest_source", False) else "off"
        source_count = len(campaign.latest_sources or []) if getattr(campaign, "use_latest_source", False) else len(campaign.message_links)
        source_label = "Source feeds" if getattr(campaign, "use_latest_source", False) else "Message links"
        return (
            f"{icon} <b>{html.escape(campaign.name)}</b>\n"
            f"<code>{campaign.id}</code>\n\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Total sent:</b> {sent_total}\n"
            f"<b>Next send:</b> {html.escape(next_text)}\n"
            f"<b>Rate:</b> {per_hour:.1f}/hour | {per_day:.1f}/24h\n"
            f"<b>Targets:</b> {len(campaign.target_refs)}\n"
            f"<b>{source_label}:</b> {source_count}\n"
            f"<b>Latest-source mode:</b> {latest_mode}"
        )

    def _campaign_keyboard(self, campaign_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Start LIVE", callback_data=f"fw:campaign:action:{campaign_id}:startlive"),
                    InlineKeyboardButton(text="⏹️ Stop", callback_data=f"fw:campaign:action:{campaign_id}:stop"),
                ],
                [
                    InlineKeyboardButton(text="⏸️ Pause", callback_data=f"fw:campaign:action:{campaign_id}:pause"),
                    InlineKeyboardButton(text="▶️ Resume", callback_data=f"fw:campaign:action:{campaign_id}:resume"),
                ],
                [
                    InlineKeyboardButton(text="✅ Enable", callback_data=f"fw:campaign:action:{campaign_id}:enable"),
                    InlineKeyboardButton(text="🚫 Disable", callback_data=f"fw:campaign:action:{campaign_id}:disable"),
                ],
                [
                    InlineKeyboardButton(text="📝 Rename", callback_data=f"fw:campaign:manage:{campaign_id}:rename"),
                    InlineKeyboardButton(text="📣 Client Report", callback_data=f"fw:report:ad:{campaign_id}"),
                ],
                [
                    InlineKeyboardButton(text="🎯 Targets", callback_data=f"fw:campaign:manage:{campaign_id}:targets"),
                    InlineKeyboardButton(text="✉️ Messages", callback_data=f"fw:campaign:manage:{campaign_id}:messages"),
                ],
                [
                    InlineKeyboardButton(text="📥 Sources", callback_data=f"fw:section:{campaign_id}:sources"),
                    InlineKeyboardButton(text="🗑️ Delete", callback_data=f"fw:campaign:manage:{campaign_id}:deleteask"),
                ],
                [
                    InlineKeyboardButton(text="⏱️ Interval", callback_data=f"fw:section:{campaign_id}:pacing"),
                    InlineKeyboardButton(text="🗓️ Schedule", callback_data=f"fw:section:{campaign_id}:schedule"),
                ],
                [
                    InlineKeyboardButton(text="📏 Limits", callback_data=f"fw:section:{campaign_id}:limits"),
                    InlineKeyboardButton(text="🧠 Delivery", callback_data=f"fw:section:{campaign_id}:modes"),
                ],
                [
                    InlineKeyboardButton(text="🚫 Disabled Targets", callback_data=f"fw:campaign:action:{campaign_id}:disabled"),
                    InlineKeyboardButton(text="🧹 Clear Disabled", callback_data=f"fw:campaign:action:{campaign_id}:cleardisabled"),
                ],
                [
                    InlineKeyboardButton(text="📊 Statistics", callback_data=f"fw:stats:ad:{campaign_id}"),
                    InlineKeyboardButton(text="🧾 Logs", callback_data=f"fw:logs:ad:{campaign_id}"),
                ],
                [InlineKeyboardButton(text="⬅️ Ads", callback_data="fw:ads:0")],
            ]
        )
    def _campaign_section_text(self, campaign_id: str, section: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        if section == "sources":
            mode = "Latest post from source" if campaign.use_latest_source else "Fixed message links"
            return (
                f"📥 <b>Sources · {html.escape(campaign.name)}</b>\n\n"
                f"<b>Active mode:</b> {mode}\n"
                f"<b>Fixed messages:</b> {len(campaign.message_links or [])}\n"
                f"<b>Latest sources:</b> {len(campaign.latest_sources or [])}\n"
                f"<b>Source order:</b> {html.escape(str(campaign.latest_source_strategy))}\n\n"
                "Fixed mode repeatedly forwards the saved message links. Latest-source mode forwards new posts from the selected sources."
            )
        if section == "pacing":
            return (
                f"⏱️ <b>Message Interval · {html.escape(campaign.name)}</b>\n\n"
                f"Send one message every <code>{_format_interval(campaign.send_gap_min_sec, campaign.send_gap_max_sec)}</code>.\n\n"
                "Messages continue using this interval after the last destination."
            )
        if section == "schedule":
            return (
                f"🗓️ <b>Schedule · {html.escape(campaign.name)}</b>\n\n"
                f"Days: <code>{html.escape(str(campaign.schedule_days))}</code>\n"
                f"Windows: <code>{html.escape(_format_windows(campaign.schedule_windows))}</code>\n"
                f"Weekday windows: <code>{html.escape(_format_windows(campaign.schedule_windows_weekday))}</code>\n"
                f"Weekend windows: <code>{html.escape(_format_windows(campaign.schedule_windows_weekend))}</code>\n"
                f"Sleep start: <code>{html.escape(str(campaign.sleep_start or '-'))}</code>\n"
                f"Sleep end: <code>{html.escape(str(campaign.sleep_end or '-'))}</code>"
            )
        if section == "limits":
            return (
                f"📏 <b>Limits · {html.escape(campaign.name)}</b>\n\n"
                f"Daily cap: <code>{html.escape(str(campaign.daily_cap or '-'))}</code>\n"
                f"Max msgs/hour: <code>{html.escape(str(campaign.max_msgs_per_hour or '-'))}</code>\n"
                f"Per-target daily cap: <code>{html.escape(str(campaign.per_target_daily_cap or '-'))}</code>\n"
                f"Per-target cooldown: <code>{_format_duration(campaign.per_target_cooldown_sec) if campaign.per_target_cooldown_sec else '-'}</code>"
            )
        if section == "modes":
            return (
                f"🧠 <b>Delivery · {html.escape(campaign.name)}</b>\n\n"
                f"<b>Message order:</b> {html.escape(str(campaign.message_strategy))}\n"
                f"<b>Destination order:</b> {html.escape(str(campaign.target_strategy))}\n"
                f"<b>Adaptive error delay:</b> {'On' if campaign.adaptive_backoff_enabled else 'Off'}\n"
                f"<b>Bot alerts:</b> {html.escape(str(campaign.bot_alert_mode or 'default'))}\n"
                f"<b>Summary every:</b> {html.escape(str(campaign.bot_alert_every_n or '-'))}\n"
                f"<b>Warm-up:</b> {'On' if campaign.warmup_enabled else 'Off'}\n"
                f"<b>Warm-up duration:</b> {html.escape(str(campaign.warmup_minutes or '-'))} minutes\n"
                f"<b>Warm-up speed:</b> {campaign.warmup_start_multiplier}x → {campaign.warmup_end_multiplier}x"
            )
        raise ValueError(f"Unknown section: {section}")

    def _campaign_section_keyboard(self, campaign_id: str, section: str) -> InlineKeyboardMarkup:
        if section == "sources":
            rows = [
                [InlineKeyboardButton(text="🔄 Switch Source Mode", callback_data=f"fw:campaign:toggle:{campaign_id}:use_latest_source")],
                [
                    InlineKeyboardButton(text="✉️ Fixed Messages", callback_data=f"fw:campaign:manage:{campaign_id}:messages"),
                    InlineKeyboardButton(text="📡 Latest Sources", callback_data=f"fw:campaign:edit:{campaign_id}:latest_sources:links"),
                ],
                [
                    InlineKeyboardButton(text="💾 Saved Messages", callback_data=f"fw:campaign:preset:{campaign_id}:saved"),
                    InlineKeyboardButton(text="🔁 Source Order", callback_data=f"fw:campaign:cycle:{campaign_id}:latest_source_strategy"),
                ],
            ]
        elif section == "pacing":
            rows = [
                [
                    InlineKeyboardButton(text="⏱️ Change Interval", callback_data=f"fw:campaign:edit:{campaign_id}:message_interval:duration_range"),
                ],
            ]
        elif section == "schedule":
            rows = [
                [InlineKeyboardButton(text="Cycle Days", callback_data=f"fw:campaign:cycle:{campaign_id}:schedule_days")],
                [
                    InlineKeyboardButton(text="Windows", callback_data=f"fw:campaign:edit:{campaign_id}:schedule_windows:windows"),
                    InlineKeyboardButton(text="Weekday", callback_data=f"fw:campaign:edit:{campaign_id}:schedule_windows_weekday:windows"),
                ],
                [
                    InlineKeyboardButton(text="Weekend", callback_data=f"fw:campaign:edit:{campaign_id}:schedule_windows_weekend:windows"),
                    InlineKeyboardButton(text="Sleep Start", callback_data=f"fw:campaign:edit:{campaign_id}:sleep_start:time"),
                ],
                [InlineKeyboardButton(text="Sleep End", callback_data=f"fw:campaign:edit:{campaign_id}:sleep_end:time")],
            ]
        elif section == "limits":
            rows = [
                [
                    InlineKeyboardButton(text="Daily Cap", callback_data=f"fw:campaign:edit:{campaign_id}:daily_cap:optional_int"),
                    InlineKeyboardButton(text="Msgs/Hour", callback_data=f"fw:campaign:edit:{campaign_id}:max_msgs_per_hour:optional_int"),
                ],
                [
                    InlineKeyboardButton(text="Per-Target Cap", callback_data=f"fw:campaign:edit:{campaign_id}:per_target_daily_cap:optional_int"),
                    InlineKeyboardButton(text="Cooldown", callback_data=f"fw:campaign:edit:{campaign_id}:pt_cooldown:optional_duration"),
                ],
            ]
        elif section == "modes":
            rows = [
                [
                    InlineKeyboardButton(text="Latest Source", callback_data=f"fw:campaign:toggle:{campaign_id}:use_latest_source"),
                    InlineKeyboardButton(text="Source Links", callback_data=f"fw:campaign:edit:{campaign_id}:latest_sources:links"),
                ],
                [
                    InlineKeyboardButton(text="Msg Strategy", callback_data=f"fw:campaign:cycle:{campaign_id}:message_strategy"),
                    InlineKeyboardButton(text="Target Strategy", callback_data=f"fw:campaign:cycle:{campaign_id}:target_strategy"),
                ],
                [
                    InlineKeyboardButton(text="Backoff", callback_data=f"fw:campaign:toggle:{campaign_id}:adaptive_backoff_enabled"),
                    InlineKeyboardButton(text="Alert Mode", callback_data=f"fw:campaign:cycle:{campaign_id}:bot_alert_mode"),
                ],
                [
                    InlineKeyboardButton(text="Alert Every N", callback_data=f"fw:campaign:edit:{campaign_id}:bot_alert_every_n:optional_int"),
                    InlineKeyboardButton(text="Warm-up", callback_data=f"fw:campaign:toggle:{campaign_id}:warmup_enabled"),
                ],
                [
                    InlineKeyboardButton(text="Warm-up Min", callback_data=f"fw:campaign:edit:{campaign_id}:warmup_minutes:optional_int"),
                    InlineKeyboardButton(text="Warm-up Start", callback_data=f"fw:campaign:edit:{campaign_id}:warmup_start_multiplier:float"),
                ],
                [InlineKeyboardButton(text="Warm-up End", callback_data=f"fw:campaign:edit:{campaign_id}:warmup_end_multiplier:float")],
            ]
        else:
            rows = []
        rows.append(
            [
                InlineKeyboardButton(text="⬅️ Back To Ad", callback_data=f"fw:ad:{campaign_id}"),
                InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home"),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _saved_targets_lines(self, targets: list[DestinationTarget], *, limit: int = 40) -> list[str]:
        lines = [f"{index}. {_target_label(target)}" for index, target in enumerate(targets[:limit], start=1)]
        if len(targets) > limit:
            lines.append(f"...and {len(targets) - limit} more saved targets.")
        return lines

    def _campaign_targets_text(self, campaign_id: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        lines = [f"🎯 <b>Targets · {html.escape(campaign.name)}</b>", ""]
        if not campaign.target_refs:
            lines.append("No targets are assigned to this ad yet.")
        else:
            lines.append(f"Current targets: <code>{len(campaign.target_refs)}</code>")
            for index, target in enumerate(campaign.target_refs[:25], start=1):
                lines.append(f"{index}. {html.escape(_target_label(target))}")
            if len(campaign.target_refs) > 25:
                lines.append(f"...and {len(campaign.target_refs) - 25} more.")
        saved_count = len(load_targets())
        lines.append("")
        lines.append(f"Saved target pool: <code>{saved_count}</code>")
        lines.append("Use Add, Replace, Remove, or Delay below.")
        return "\n".join(lines)

    def _campaign_targets_keyboard(self, campaign_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Add", callback_data=f"fw:campaign:target:{campaign_id}:add"),
                    InlineKeyboardButton(text="🔁 Replace", callback_data=f"fw:campaign:target:{campaign_id}:replace"),
                ],
                [
                    InlineKeyboardButton(text="➖ Remove", callback_data=f"fw:campaign:target:{campaign_id}:remove"),
                    InlineKeyboardButton(text="⏳ Delay", callback_data=f"fw:campaign:target:{campaign_id}:delay"),
                ],
                [
                    InlineKeyboardButton(text="⬅️ Back To Ad", callback_data=f"fw:ad:{campaign_id}"),
                    InlineKeyboardButton(text="🏠 Main Menu", callback_data="fw:home"),
                ],
            ]
        )

    def _delete_confirm_text(self, campaign_id: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        return (
            f"🗑️ <b>Delete Ad</b>\n\n"
            f"Ad: <b>{html.escape(campaign.name)}</b>\n"
            f"ID: <code>{campaign.id}</code>\n\n"
            "This will stop the ad if it is running, remove the saved ad definition, and delete its state file.\n"
            "Existing history files stay on disk."
        )

    def _delete_confirm_keyboard(self, campaign_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗑️ Delete Now", callback_data=f"fw:campaign:manage:{campaign_id}:delete"),
                    InlineKeyboardButton(text="⬅️ Cancel", callback_data=f"fw:ad:{campaign_id}"),
                ]
            ]
        )

    def _target_prompt(self, campaign_id: str, action: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        if action in {"add", "replace"}:
            saved_targets = load_targets()
            if not saved_targets:
                raise ValueError("No saved targets exist yet. Add them once in the interactive app first.")
            lines = [
                f"🎯 <b>{'Add' if action == 'add' else 'Replace'} targets · {html.escape(campaign.name)}</b>",
                "",
                "Reply with target numbers like <code>1,3,5-7</code> or <code>all</code>.",
                "",
                *[html.escape(line) for line in self._saved_targets_lines(saved_targets)],
            ]
            return "\n".join(lines)
        current_targets = campaign.target_refs or []
        if not current_targets:
            raise ValueError("This ad has no targets to edit.")
        if action == "delay":
            lines = [
                f"⏳ <b>Destination delay · {html.escape(campaign.name)}</b>",
                "",
                "Set an additional wait after sending to selected destinations.",
                "Reply as <code>1,3 | 30m</code> or <code>all | 2h</code>.",
                "Use <code>1,3 | none</code> to clear the extra delay.",
                "",
            ]
            for index, target in enumerate(current_targets[:40], start=1):
                lines.append(f"{index}. {html.escape(_target_label(target))}")
            if len(current_targets) > 40:
                lines.append(f"...and {len(current_targets) - 40} more.")
            return "\n".join(lines)
        if action != "remove":
            raise ValueError(f"Unknown target action: {action}")
        lines = [
            f"➖ <b>Remove targets · {html.escape(campaign.name)}</b>",
            "",
            "Reply with the current target numbers to remove, like <code>1,2</code> or <code>all</code>.",
            "",
        ]
        for index, target in enumerate(current_targets[:40], start=1):
            lines.append(f"{index}. {html.escape(_target_label(target))}")
        if len(current_targets) > 40:
            lines.append(f"...and {len(current_targets) - 40} more.")
        return "\n".join(lines)

    def _apply_compose_input(self, pending: PendingInput, raw: str) -> InputResult:
        draft = dict(pending.extra.get("draft") or {})
        settings = load_settings()

        if pending.field == "name":
            name = raw.strip()
            if not name:
                raise ValueError("Ad name cannot be empty.")
            draft["name"] = name
            prompt = (
                "🧭 <b>Create New Ad</b>\n\n"
                "Reply with <code>links</code> for saved Telegram message links, or <code>latest</code> to forward the newest post from source feeds."
            )
            return InputResult(
                prompt,
                PendingInput("compose", "mode", "text", None, "ads", prompt, {"draft": draft}),
            )

        if pending.field == "mode":
            mode = raw.strip().casefold()
            if mode not in {"links", "latest"}:
                raise ValueError("Reply with 'links' or 'latest'.")
            draft["use_latest_source"] = mode == "latest"
            if mode == "latest":
                saved_lines = _saved_sources_lines()
                saved_text = "\n".join(saved_lines) if saved_lines else "No saved sources yet. Add one from Groups & Sources."
                prompt = (
                    "📥 <b>Create New Ad · Sources</b>\n\n"
                    "Reply with saved source numbers such as <code>1,3</code> or <code>all</code>.\n"
                    "You can also send a channel, group, private-chat reference, or <code>saved</code> for Saved Messages.\n"
                    "The logged-in Telegram account must be able to read the source.\n\n"
                    f"<b>Saved sources</b>\n{saved_text}"
                )
            else:
                prompt = (
                    "🔗 <b>Create New Ad · Messages</b>\n\n"
                    "Send one or more Telegram message links, one per line.\n\n"
                    "When you finish, send the whole list in one message."
                )
            return InputResult(
                prompt,
                PendingInput("compose", "sources", "links", None, "ads", prompt, {"draft": draft}),
            )

        if pending.field == "sources":
            if draft.get("use_latest_source"):
                draft["latest_sources"] = _parse_source_input(raw)
                draft["message_links"] = []
            else:
                draft["message_links"] = _parse_message_links(raw)
                draft["latest_sources"] = []
            saved_targets = load_targets()
            if not saved_targets:
                raise ValueError("No saved targets exist yet. Add them once in the interactive app first.")
            prompt_lines = [
                "🎯 <b>Create New Ad</b>",
                "",
                "Reply with target numbers like <code>1,3,5-7</code> or <code>all</code>.",
                "",
                *[html.escape(line) for line in self._saved_targets_lines(saved_targets)],
            ]
            prompt = "\n".join(prompt_lines)
            return InputResult(
                prompt,
                PendingInput("compose", "targets", "selection", None, "ads", prompt, {"draft": draft}),
            )

        if pending.field == "targets":
            saved_targets = load_targets()
            idxs = _parse_selection(raw, max_index=len(saved_targets))
            if not idxs:
                raise ValueError("Select at least one target.")
            draft["target_refs"] = [_target_dict(saved_targets[index - 1]) for index in idxs]
            default_interval = _format_interval(
                settings.default_send_gap_min_sec,
                settings.default_send_gap_max_sec,
            )
            prompt = (
                "⏱️ <b>Create New Ad</b>\n\n"
                "How often should one message be sent?\n\n"
                "Send a duration such as <code>30m</code>, <code>2h</code>, or <code>12h</code>.\n"
                "For a random interval, use a range such as <code>1h-2h</code>.\n"
                f"Send <code>default</code> to use <code>{default_interval}</code>."
            )
            return InputResult(
                prompt,
                PendingInput("compose", "pacing", "duration_range", None, "ads", prompt, {"draft": draft}),
            )

        if pending.field != "pacing":
            raise ValueError(f"Unknown compose step: {pending.field}")

        if raw.strip().casefold() in {"default", "defaults"}:
            send_min = int(settings.default_send_gap_min_sec)
            send_max = int(settings.default_send_gap_max_sec)
        else:
            send_min, send_max = _parse_duration_range(raw)

        campaign = Campaign(
            id=new_campaign_id(),
            name=str(draft["name"]),
            message_links=list(draft.get("message_links") or []),
            target_refs=list(draft.get("target_refs") or []),
            send_gap_min_sec=send_min,
            send_gap_max_sec=send_max,
            batch_gap_min_sec=0,
            batch_gap_max_sec=0,
            use_latest_source=bool(draft.get("use_latest_source")),
            latest_sources=list(draft.get("latest_sources") or []),
            latest_source_strategy="round_robin",
            schedule_days=str(getattr(settings, "default_schedule_days", "all") or "all"),
            schedule_windows=_clone_windows(getattr(settings, "default_schedule_windows", None)),
            schedule_windows_weekday=None,
            schedule_windows_weekend=None,
            sleep_start=str(getattr(settings, "default_sleep_start", "") or ""),
            sleep_end=str(getattr(settings, "default_sleep_end", "") or ""),
            message_strategy="shuffle_bag",
            target_strategy="shuffle_bag",
            daily_cap=None,
            per_target_cooldown_sec=None,
            max_msgs_per_hour=None,
            per_target_daily_cap=None,
            enabled=True,
            adaptive_backoff_enabled=True,
            warmup_enabled=False,
            warmup_minutes=None,
            warmup_start_multiplier=2.0,
            warmup_end_multiplier=1.0,
            bot_alert_mode=None,
            bot_alert_every_n=None,
        )
        save_campaign(campaign)
        return InputResult(
            f"✅ Saved new ad <b>{html.escape(campaign.name)}</b>.\n\n{self._campaign_text(campaign.id)}"
        )

    def _apply_target_input(self, pending: PendingInput, raw: str) -> InputResult:
        campaign = self._campaign_or_raise(str(pending.campaign_id))
        action = pending.field

        if action in {"add", "replace"}:
            saved_targets = load_targets()
            idxs = _parse_selection(raw, max_index=len(saved_targets))
            if not idxs:
                raise ValueError("Select at least one saved target.")
            chosen = [_target_dict(saved_targets[index - 1]) for index in idxs]
            if action == "replace":
                campaign.target_refs = chosen
            else:
                merged: list[dict[str, Any]] = list(campaign.target_refs or [])
                seen = {_target_key(item) for item in merged}
                for target in chosen:
                    key = _target_key(target)
                    if key in seen:
                        continue
                    merged.append(target)
                    seen.add(key)
                campaign.target_refs = merged
        elif action == "remove":
            current_targets = list(campaign.target_refs or [])
            idxs = _parse_selection(raw, max_index=len(current_targets))
            if not idxs:
                raise ValueError("Select at least one current target to remove.")
            kill = set(idxs)
            campaign.target_refs = [target for index, target in enumerate(current_targets, start=1) if index not in kill]
        elif action == "delay":
            if "|" not in raw:
                raise ValueError("Use target numbers and a delay, for example: 1,3 | 30m")
            selection_raw, delay_raw = [part.strip() for part in raw.split("|", 1)]
            current_targets = list(campaign.target_refs or [])
            idxs = _parse_selection(selection_raw, max_index=len(current_targets))
            if not idxs:
                raise ValueError("Select at least one current target.")
            delay = None if delay_raw.casefold() in {"none", "clear", "0"} else _parse_duration(delay_raw)
            selected = set(idxs)
            updated: list[dict[str, Any]] = []
            for index, target in enumerate(current_targets, start=1):
                item = dict(target)
                if index in selected:
                    item["extra_delay_sec"] = delay
                updated.append(item)
            campaign.target_refs = updated
        else:
            raise ValueError(f"Unknown target action: {action}")

        replace_campaign(campaign)
        return InputResult(
            f"✅ Updated targets for <b>{html.escape(campaign.name)}</b>.\n\n{self._campaign_targets_text(campaign.id)}"
        )

    async def _apply_source_input(self, pending: PendingInput, raw: str) -> InputResult:
        if pending.field != "add":
            raise ValueError(f"Unknown source action: {pending.field}")
        control = self.manager.control
        callback = getattr(control, "add_source", None) if control else None
        if callback is None:
            raise ValueError("Source management is unavailable.")
        result = await callback(raw)
        return InputResult(f"{html.escape(result)}\n\nSend /menu to return to the control center.")




    async def _run_campaign_action(self, campaign_id: str, action: str) -> str:
        self._campaign_or_raise(campaign_id)
        if action == "pause":
            ok = pause_state(PROFILES_DIR / f"state_{campaign_id}.json", campaign_id)
            return "⏸️ Ad paused." if ok else "⚠️ No running state found for this ad yet."
        if action == "resume":
            ok = resume_state(PROFILES_DIR / f"state_{campaign_id}.json", campaign_id)
            if not ok:
                return "⚠️ No running state found for this ad yet."
            start_notice = await self._control_start(campaign_id, dry=False, force=False)
            if "already running" in start_notice.casefold():
                return "▶️ Ad resumed."
            return f"▶️ Ad resumed.\n{html.escape(start_notice)}"
        if action == "stop":
            return await self._control_action("stop_running", campaign_id, "Stop control unavailable.")
        if action == "enable":
            return await self._control_action("enable_ad", campaign_id, "Enable control unavailable.")
        if action == "disable":
            return await self._control_action("disable_ad", campaign_id, "Disable control unavailable.")
        if action == "disabled":
            return await self._control_action("list_disabled", campaign_id, "Disabled-target view unavailable.")
        if action == "cleardisabled":
            return await self._control_action("clear_disabled", campaign_id, "Clear-disabled action unavailable.")
        if action == "startdry":
            return await self._control_start(campaign_id, dry=True, force=True)
        if action == "startlive":
            return await self._control_start(campaign_id, dry=False, force=True)
        if action == "forcedry":
            return await self._control_start(campaign_id, dry=True, force=True)
        if action == "forcelive":
            return await self._control_start(campaign_id, dry=False, force=True)
        raise ValueError(f"Unknown action: {action}")

    async def _control_start(self, campaign_id: str, *, dry: bool, force: bool) -> str:
        control = self.manager.control
        if control is None:
            return "Control interface is unavailable."
        return await control.start_ad(campaign_id, dry, force)

    async def _control_action(self, field: str, campaign_id: str, fallback: str) -> str:
        control = self.manager.control
        if control is None:
            return fallback
        callback = getattr(control, field, None)
        if callback is None:
            return fallback
        return await callback(campaign_id)

    async def _reload_campaign_after_change(self, campaign_id: str) -> str:
        control = self.manager.control
        callback = getattr(control, "reload_ad", None) if control is not None else None
        if callback is None:
            return "Saved. The change will apply when this ad starts."
        return await callback(campaign_id)

    def _campaign_prompt(self, campaign_id: str, field: str, parser: str) -> str:
        campaign = self._campaign_or_raise(campaign_id)
        current = getattr(campaign, field, None)
        if field == "message_interval":
            return (
                f"⏱️ <b>Message Interval · {html.escape(campaign.name)}</b>\n\n"
                f"Current: <code>{_format_interval(campaign.send_gap_min_sec, campaign.send_gap_max_sec)}</code>\n\n"
                "Send <code>30m</code>, <code>2h</code>, or <code>12h</code>.\n"
                "You can also use a random range such as <code>1h-2h</code>."
            )
        if field == "per_target_cooldown_sec":
            current_cooldown = _format_duration(current) if current else "none"
            return (
                f"⏳ <b>Target Cooldown · {html.escape(campaign.name)}</b>\n\n"
                f"Current: <code>{current_cooldown}</code>\n\n"
                "Send <code>30m</code> or <code>2h</code>. Send <code>none</code> to clear it."
            )
        if field == "latest_sources":
            saved_lines = _saved_sources_lines()
            saved_text = "\n".join(saved_lines) if saved_lines else "No saved sources yet."
            return (
                f"📡 Update <b>{html.escape(campaign.name)}</b>\n"
                "Field: <code>latest_sources</code>\n"
                f"Current count: <code>{len(campaign.latest_sources or [])}</code>\n\n"
                "Send saved source numbers such as <code>1,3</code>, <code>all</code>, or one direct source per line.\n"
                "Accepted values:\n"
                "• <code>@channelusername</code>\n"
                "• a readable group or private-chat username\n"
                "• <code>https://t.me/channelusername</code>\n"
                "• <code>https://t.me/c/123456/1</code>\n"
                "• <code>saved</code> or <code>me</code> for Saved Messages\n\n"
                f"<b>Saved sources</b>\n{saved_text}\n\n"
                f"<b>Current list</b>\n{_format_source_refs(campaign.latest_sources)}"
            )
        if field == "message_links":
            current_links = [str(item).strip() for item in (campaign.message_links or []) if str(item).strip()]
            preview = "\n".join(f"• <code>{html.escape(item)}</code>" for item in current_links[:8]) or "-"
            if len(current_links) > 8:
                preview += f"\n• <code>...and {len(current_links) - 8} more</code>"
            return (
                f"✉️ Update <b>{html.escape(campaign.name)}</b>\n"
                "Field: <code>message_links</code>\n"
                f"Current count: <code>{len(current_links)}</code>\n\n"
                "Send one Telegram message link per line.\n\n"
                f"Current list:\n{preview}"
            )
        return (
            f"✍️ Update <b>{html.escape(campaign.name)}</b>\n"
            f"Field: <code>{html.escape(field)}</code>\n"
            f"Current: <code>{html.escape(str(current))}</code>\n\n"
            f"{self._parser_help(parser)}"
        )

    def _settings_prompt(self, field: str, parser: str) -> str:
        settings = load_settings()
        current = getattr(settings, field, None)
        label = self._settings_field_label(field)
        duration_fields = {
            "latest_source_poll_sec", "default_send_gap_min_sec", "default_send_gap_max_sec",
            "cooldown_timeout_sec",
        }
        current_text = _format_duration(current) if field in duration_fields and current else str(current)
        return (
            f"✍️ <b>Change {html.escape(label)}</b>\n\n"
            f"Current value: <code>{html.escape(current_text)}</code>\n\n"
            f"{self._parser_help(parser)}"
        )

    def _settings_field_label(self, field: str) -> str:
        labels = {
            "global_quiet_start": "quiet-hours start",
            "global_quiet_end": "quiet-hours end",
            "latest_source_poll_sec": "source check interval",
            "default_send_gap_min_sec": "new-ad minimum interval",
            "default_send_gap_max_sec": "new-ad maximum interval",
            "retry_transient_count": "temporary retry count",
            "retry_transient_base_delay_sec": "delay between retries",
            "cooldown_timeout_sec": "timeout wait",
            "max_error_streak_pause": "pause threshold",
            "auto_disable_target_error_count": "disable threshold",
            "account_rate_multiplier_default": "account speed multiplier",
            "global_jitter_min_sec": "minimum random delay",
            "global_jitter_max_sec": "maximum random delay",
            "global_min_send_gap_sec": "absolute minimum send gap",
            "global_max_msgs_per_hour": "global messages-per-hour limit",
            "live_update_every_sec": "menu status refresh",
        }
        return labels.get(field, field.replace("_", " "))

    def _parser_help(self, parser: str) -> str:
        mapping = {
            "int": "Send a whole number.",
            "float": "Send a number like 1.5.",
            "optional_int": "Send a whole number, or <code>none</code> to clear it.",
            "optional_duration": "Send 30m, 2h, or none to clear it.",
            "duration": "Send a duration such as <code>5m</code>, <code>30m</code>, or <code>2h</code>.",
            "time": "Send time as <code>HH:MM</code>.",
            "text": "Send the new text value.",
            "windows": "Send windows as <code>09:00-12:00,14:00-18:00</code> or <code>none</code>.",
            "links": "Send one readable Telegram source per line. Use a username, link, numeric peer ID, or <code>saved</code> for Saved Messages.",
            "quad_ints": "Send four comma-separated numbers.",
            "duration_range": "Send 30m, 2h, 12h, or a range such as 1h-2h.",
        }
        return mapping.get(parser, "Send the new value.")

    def _apply_campaign_input(self, pending: PendingInput, raw: str) -> str:
        campaign = self._campaign_or_raise(str(pending.campaign_id))
        expected_parser = _CAMPAIGN_EDIT_PARSERS.get(pending.field)
        if expected_parser is None or pending.parser != expected_parser:
            raise ValueError("This ad setting is not editable.")
        if pending.field == "message_interval":
            minimum, maximum = _parse_duration_range(raw)
            campaign.send_gap_min_sec = minimum
            campaign.send_gap_max_sec = maximum
            campaign.batch_gap_min_sec = 0
            campaign.batch_gap_max_sec = 0
            replace_campaign(campaign)
            return (
                f"✅ Message interval for <b>{html.escape(campaign.name)}</b> is now "
                f"<code>{_format_interval(minimum, maximum)}</code>."
            )
        if pending.field == "latest_sources":
            value = _parse_source_input(raw)
        elif pending.field == "message_links":
            value = _parse_message_links(raw)
        else:
            value = self._parse_input_value(raw, pending.parser)
        if pending.field == "name":
            value = str(value).strip()
            if not value:
                raise ValueError("Ad name cannot be empty.")
        optional_positive_fields = {
            "daily_cap",
            "max_msgs_per_hour",
            "per_target_daily_cap",
            "per_target_cooldown_sec",
            "bot_alert_every_n",
            "warmup_minutes",
        }
        if pending.field in optional_positive_fields and value is not None and int(value) <= 0:
            raise ValueError("Use a number greater than zero, or send none to clear it.")
        if pending.field in {"warmup_start_multiplier", "warmup_end_multiplier"} and float(value) <= 0:
            raise ValueError("The warm-up multiplier must be greater than zero.")
        setattr(campaign, pending.field, value)
        if pending.field == "use_latest_source" and value and not list(campaign.latest_sources or []):
            raise ValueError("Add at least one latest-source link before enabling latest-source mode.")
        replace_campaign(campaign)
        return f"✅ Saved <code>{html.escape(pending.field)}</code> for <b>{html.escape(campaign.name)}</b>."

    def _apply_settings_input(self, pending: PendingInput, raw: str) -> str:
        settings = load_settings()
        value = self._parse_input_value(raw, pending.parser)
        setattr(settings, pending.field, value)
        save_settings(settings)
        control = self.manager.control
        if control is not None and getattr(control, "reload_settings", None) is not None:
            try:
                control.reload_settings()
            except Exception:
                pass
        return f"✅ <b>{html.escape(self._settings_field_label(pending.field).capitalize())}</b> updated."

    def _parse_input_value(self, raw: str, parser: str) -> Any:
        if parser == "int":
            return int(raw)
        if parser == "float":
            return float(raw)
        if parser == "optional_int":
            if raw.casefold() in {"none", "clear", "0"}:
                return None
            return int(raw)
        if parser == "optional_duration":
            if raw.casefold() in {"none", "clear", "0"}:
                return None
            return _parse_duration(raw)
        if parser == "duration":
            return _parse_duration(raw)
        if parser == "time":
            parts = raw.split(":")
            if len(parts) != 2:
                raise ValueError("Use HH:MM.")
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Use HH:MM.")
            return f"{hour:02d}:{minute:02d}"
        if parser == "windows":
            return _parse_windows(raw)
        if parser == "links":
            return _parse_latest_sources(raw)
        if parser == "text":
            return raw.strip()
        if parser == "quad_ints":
            values = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
            if len(values) != 4:
                raise ValueError("Send four comma-separated numbers.")
            return [int(item) for item in values]
        return raw

    def _campaign_or_raise(self, campaign_id: str) -> Campaign:
        campaign = get_campaign(campaign_id)
        if campaign is None:
            raise ValueError(f"Ad not found: {campaign_id}")
        return campaign

    async def _stop_running_if_needed(self, campaign_id: str) -> str | None:
        control = self.manager.control
        if control is None or getattr(control, "stop_running", None) is None:
            return None
        try:
            notice = await control.stop_running(campaign_id)
        except Exception as exc:
            return f"⚠️ Could not stop running task cleanly: {html.escape(type(exc).__name__)}: {html.escape(str(exc))}"
        normalized = (notice or "").strip()
        if not normalized or "Ad not running" in normalized:
            return None
        return f"⏹️ {html.escape(normalized)}"

    def _delete_campaign_state(self, campaign_id: str) -> None:
        state_path = PROFILES_DIR / f"state_{campaign_id}.json"
        try:
            state_path.unlink(missing_ok=True)
        except Exception:
            pass

    async def _edit(self, query: Any, text: str, markup: InlineKeyboardMarkup) -> None:
        if query.message is None:
            return
        try:
            await query.message.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                raise
