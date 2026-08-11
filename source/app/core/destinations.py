from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from telethon.tl.types import Channel, Chat


@dataclass
class Destination:
    id: int
    title: str
    username: Optional[str]
    kind: str     # "group" | "channel" | "unknown"
    status: str   # "likely" | "unknown" | "blocked"
    reason: Optional[str] = None
    peer_type: str = "channel"  # "channel" | "chat"
    sendable: bool = False

    # Stars / paid messages (best-effort)
    paid_message_stars: Optional[int] = None

    # Topics support (forum supergroups)
    is_forum: bool = False
    selected_topic_id: Optional[int] = None
    selected_topic_title: Optional[str] = None
    selected_topic_top_message: Optional[int] = None
    topics: list[dict] = field(default_factory=list)
    topic_error: Optional[str] = None


def classify_dialog(entity) -> str:
    # Telethon types:
    # - Chat: basic group
    # - Channel: supergroup or channel (broadcast=True for channels, megagroup=True for supergroups)
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "broadcast", False):
            return "channel"
        if getattr(entity, "megagroup", False):
            return "group"
        return "unknown"
    return "unknown"


def _right_blocks_messages(rights) -> bool:
    if rights is None:
        return False
    return bool(
        getattr(rights, "send_messages", False)
        or getattr(rights, "send_plain", False)
    )


def can_send_messages(entity) -> tuple[bool, Optional[str]]:
    if bool(getattr(entity, "left", False)):
        return False, "Account left this chat"
    if bool(getattr(entity, "kicked", False)):
        return False, "Account was removed from this chat"
    if bool(getattr(entity, "deactivated", False)):
        return False, "Chat is deactivated"
    if _right_blocks_messages(getattr(entity, "banned_rights", None)):
        return False, "Account is not allowed to send messages"

    if isinstance(entity, Chat):
        if _right_blocks_messages(getattr(entity, "default_banned_rights", None)):
            return False, "Members cannot send messages"
        return True, None

    if isinstance(entity, Channel):
        if getattr(entity, "broadcast", False):
            if bool(getattr(entity, "creator", False)):
                return True, None
            admin = getattr(entity, "admin_rights", None)
            if admin is not None and bool(getattr(admin, "post_messages", False)):
                return True, None
            return False, "Account cannot post in this channel"

        if getattr(entity, "megagroup", False):
            if bool(getattr(entity, "creator", False)) or getattr(entity, "admin_rights", None) is not None:
                return True, None
            if _right_blocks_messages(getattr(entity, "default_banned_rights", None)):
                return False, "Members cannot send messages"
            return True, None

    return False, "Unsupported chat type"


async def sync_destinations(tg_client) -> List[Destination]:
    destinations: List[Destination] = []

    async for dialog in tg_client.iter_dialogs():
        entity = dialog.entity

        title = getattr(entity, "title", None)
        if not title:
            continue

        kind = classify_dialog(entity)

        # Detect forum-enabled supergroups (topics)
        is_forum = bool(getattr(entity, "forum", False))

        # Best-effort paid messages indicator (Stars)
        paid_message_stars = getattr(entity, "send_paid_messages_stars", None)
        if paid_message_stars is not None:
            try:
                paid_message_stars = int(paid_message_stars)
            except Exception:
                paid_message_stars = None

        sendable, reason = can_send_messages(entity)
        status = "likely" if sendable else "blocked"
        peer_type = "chat" if isinstance(entity, Chat) else "channel"

        destinations.append(
            Destination(
                id=int(entity.id),
                title=str(title),
                username=getattr(entity, "username", None),
                kind=kind,
                status=status,
                reason=reason,
                peer_type=peer_type,
                sendable=sendable,
                paid_message_stars=paid_message_stars,
                is_forum=is_forum,
                # topic selections start empty, user will set them later
                selected_topic_id=None,
                selected_topic_title=None,
                selected_topic_top_message=None,
            )
        )

    # Sort groups first, then channels, then by title
    destinations.sort(key=lambda d: (d.kind != "group", d.title.lower()))
    return destinations
