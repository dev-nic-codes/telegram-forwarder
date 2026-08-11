from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from app.core.campaigns import list_campaigns, replace_campaign
from app.core.destinations import Destination, sync_destinations
from app.core.targets import DestinationTarget, load_targets, save_targets
from app.core.topics import fetch_forum_topics
from app.utils.paths import DESTINATIONS_CACHE, PROFILES_DIR
from app.utils.storage import load_json, save_json


GROUP_SYNC_STATUS = PROFILES_DIR / "group_sync_status.json"
GROUP_SELECTION = PROFILES_DIR / "group_selection.json"
GROUP_TOPIC_SELECTION = PROFILES_DIR / "group_topic_selection.json"


@dataclass
class GroupSyncReport:
    scanned_dialogs: int
    sendable_groups: int
    selected_groups: int
    excluded_groups: int
    added_groups: int
    removed_targets: int
    updated_targets: int
    campaigns_updated: int
    campaign_targets_removed: int
    changed_campaign_ids: list[str]
    restart_campaign_ids: list[str]
    scanned_at: str
    forum_groups: int = 0
    topics_cached: int = 0
    topic_errors: int = 0


def load_group_sync_status() -> dict:
    value = load_json(GROUP_SYNC_STATUS, default={})
    return value if isinstance(value, dict) else {}


def load_excluded_group_ids() -> set[int]:
    value = load_json(GROUP_SELECTION, default={})
    raw_ids = value.get("excluded_group_ids", []) if isinstance(value, dict) else []
    result: set[int] = set()
    for raw_id in raw_ids:
        try:
            result.add(int(raw_id))
        except (TypeError, ValueError):
            continue
    return result


def save_excluded_group_ids(group_ids: set[int]) -> None:
    save_json(
        GROUP_SELECTION,
        {"excluded_group_ids": sorted({int(group_id) for group_id in group_ids})},
    )


def set_group_enabled(group_id: int, enabled: bool) -> bool:
    excluded = load_excluded_group_ids()
    changed = False
    if enabled and group_id in excluded:
        excluded.remove(group_id)
        changed = True
    elif not enabled and group_id not in excluded:
        excluded.add(group_id)
        changed = True
    if changed:
        save_excluded_group_ids(excluded)
    return changed


def load_group_topic_selection() -> dict[int, set[int]]:
    value = load_json(GROUP_TOPIC_SELECTION, default={})
    raw_groups = value.get("groups", {}) if isinstance(value, dict) else {}
    result: dict[int, set[int]] = {}
    if not isinstance(raw_groups, dict):
        return result
    for raw_group_id, raw_topic_ids in raw_groups.items():
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            continue
        topic_ids: set[int] = set()
        for raw_topic_id in raw_topic_ids if isinstance(raw_topic_ids, list) else []:
            try:
                topic_ids.add(int(raw_topic_id))
            except (TypeError, ValueError):
                continue
        result[group_id] = topic_ids
    return result


def save_group_topic_selection(selection: dict[int, set[int]]) -> None:
    save_json(
        GROUP_TOPIC_SELECTION,
        {
            "groups": {
                str(group_id): sorted(int(topic_id) for topic_id in topic_ids)
                for group_id, topic_ids in sorted(selection.items())
            }
        },
    )


def set_group_topic_enabled(group_id: int, topic_id: int, enabled: bool) -> bool:
    group_id = int(group_id)
    topic_id = int(topic_id)
    selection = load_group_topic_selection()
    was_explicit = group_id in selection
    selected = selection.setdefault(group_id, set())
    changed = False
    if enabled and topic_id not in selected:
        selected.add(topic_id)
        changed = True
    elif not enabled and topic_id in selected:
        selected.remove(topic_id)
        changed = True
    if changed or not was_explicit:
        save_group_topic_selection(selection)
    return changed


def _updated_target(target: DestinationTarget, destination: Destination) -> DestinationTarget:
    return DestinationTarget(
        group_id=destination.id,
        group_title=destination.title,
        peer_type=destination.peer_type,
        topic_id=target.topic_id,
        topic_title=target.topic_title,
        topic_top_message=target.topic_top_message,
        paid_message_stars=destination.paid_message_stars,
        is_paid=bool(target.is_paid or destination.paid_message_stars),
        extra_delay_sec=target.extra_delay_sec,
    )


def _updated_campaign_target(target: dict, destination: Destination) -> dict:
    updated = dict(target)
    updated["group_id"] = int(destination.id)
    updated["group_title"] = str(destination.title)
    updated["peer_type"] = destination.peer_type
    updated["paid_message_stars"] = destination.paid_message_stars
    updated["is_paid"] = bool(target.get("is_paid") or destination.paid_message_stars)
    return updated


def _campaign_target_signature(target: dict) -> tuple[int, int | None] | None:
    try:
        group_id = int(target.get("group_id"))
    except (AttributeError, TypeError, ValueError):
        return None
    raw_topic_id = target.get("topic_id")
    if raw_topic_id is None:
        return (group_id, None)
    try:
        return (group_id, int(raw_topic_id))
    except (TypeError, ValueError):
        return None


async def sync_sendable_groups(tg_client, *, refresh_topics: bool = False) -> GroupSyncReport:
    destinations = await sync_destinations(tg_client)
    previous_cache = load_json(DESTINATIONS_CACHE, default=[])
    cached_by_group = {
        int(item["id"]): item
        for item in previous_cache
        if isinstance(item, dict) and str(item.get("id", "")).lstrip("-").isdigit()
    }
    topic_errors = 0
    for destination in destinations:
        if not destination.is_forum:
            continue
        cached = cached_by_group.get(int(destination.id), {})
        cached_topics = cached.get("topics", []) if isinstance(cached, dict) else []
        destination.topics = list(cached_topics) if isinstance(cached_topics, list) else []
        if refresh_topics or not destination.topics:
            try:
                topics = await fetch_forum_topics(tg_client, destination.id, limit=200)
                destination.topics = [
                    {
                        "topic_id": int(topic.topic_id),
                        "title": str(topic.title),
                        "top_message": int(topic.top_message),
                    }
                    for topic in topics
                    if int(topic.topic_id) > 0
                ]
                destination.topic_error = None
            except Exception as exc:
                topic_errors += 1
                destination.topic_error = f"{type(exc).__name__}: {exc}"
    save_json(DESTINATIONS_CACHE, [asdict(destination) for destination in destinations])

    all_sendable = {
        int(destination.id): destination
        for destination in destinations
        if destination.kind == "group" and destination.sendable
    }
    excluded_ids = load_excluded_group_ids()
    sendable = {
        group_id: destination
        for group_id, destination in all_sendable.items()
        if group_id not in excluded_ids
    }

    previous_targets = load_targets()
    topic_selection = load_group_topic_selection()
    previous_by_group: dict[int, list[DestinationTarget]] = {}
    for target in previous_targets:
        previous_by_group.setdefault(int(target.group_id), []).append(target)

    next_targets: list[DestinationTarget] = []
    added_groups = 0
    updated_targets = 0
    for group_id, destination in sorted(sendable.items(), key=lambda item: item[1].title.casefold()):
        existing = previous_by_group.get(group_id, [])
        if destination.is_forum and group_id in topic_selection:
            existing_by_topic = {
                int(target.topic_id): target
                for target in existing
                if target.topic_id is not None
            }
            topics_by_id = {
                int(topic["topic_id"]): topic
                for topic in destination.topics
                if isinstance(topic, dict) and str(topic.get("topic_id", "")).isdigit()
            }
            for topic_id in sorted(topic_selection[group_id]):
                topic = topics_by_id.get(topic_id)
                old_target = existing_by_topic.get(topic_id)
                if topic is None and old_target is None:
                    continue
                next_targets.append(
                    DestinationTarget(
                        group_id=group_id,
                        group_title=destination.title,
                        peer_type=destination.peer_type,
                        topic_id=topic_id,
                        topic_title=(str(topic.get("title")) if topic else old_target.topic_title),
                        topic_top_message=(
                            int(topic.get("top_message") or topic_id)
                            if topic
                            else old_target.topic_top_message
                        ),
                        paid_message_stars=destination.paid_message_stars,
                        is_paid=bool((old_target and old_target.is_paid) or destination.paid_message_stars),
                        extra_delay_sec=old_target.extra_delay_sec if old_target else None,
                    )
                )
            if not existing and topic_selection[group_id]:
                added_groups += 1
            continue
        if not existing:
            next_targets.append(
                DestinationTarget(
                    group_id=group_id,
                    group_title=destination.title,
                    peer_type=destination.peer_type,
                    paid_message_stars=destination.paid_message_stars,
                    is_paid=bool(destination.paid_message_stars),
                )
            )
            added_groups += 1
            continue
        for target in existing:
            updated = _updated_target(target, destination)
            if updated != target:
                updated_targets += 1
            next_targets.append(updated)

    next_keys = {target.key() for target in next_targets}
    removed_targets = sum(1 for target in previous_targets if target.key() not in next_keys)
    save_targets(next_targets)

    changed_campaign_ids: list[str] = []
    restart_campaign_ids: list[str] = []
    campaign_targets_removed = 0
    for campaign in list_campaigns():
        previous_refs = list(campaign.target_refs or [])
        previous_signature = [_campaign_target_signature(target) for target in previous_refs]
        next_refs: list[dict] = []
        for target in previous_refs:
            try:
                group_id = int(target.get("group_id"))
            except Exception:
                campaign_targets_removed += 1
                continue
            destination = sendable.get(group_id)
            if destination is None:
                campaign_targets_removed += 1
                continue
            next_refs.append(_updated_campaign_target(target, destination))
        if next_refs != previous_refs:
            campaign.target_refs = next_refs
            replace_campaign(campaign)
            changed_campaign_ids.append(campaign.id)
            next_signature = [_campaign_target_signature(target) for target in next_refs]
            if next_signature != previous_signature:
                restart_campaign_ids.append(campaign.id)

    scanned_at = datetime.now().isoformat()
    report = GroupSyncReport(
        scanned_dialogs=len(destinations),
        sendable_groups=len(all_sendable),
        selected_groups=len(sendable),
        excluded_groups=len(all_sendable) - len(sendable),
        added_groups=added_groups,
        removed_targets=removed_targets,
        updated_targets=updated_targets,
        campaigns_updated=len(changed_campaign_ids),
        campaign_targets_removed=campaign_targets_removed,
        changed_campaign_ids=changed_campaign_ids,
        restart_campaign_ids=restart_campaign_ids,
        scanned_at=scanned_at,
        forum_groups=sum(1 for destination in all_sendable.values() if destination.is_forum),
        topics_cached=sum(len(destination.topics) for destination in destinations if destination.is_forum),
        topic_errors=topic_errors,
    )
    save_json(GROUP_SYNC_STATUS, asdict(report))
    return report
