from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.utils.paths import PROFILES_DIR
from app.utils.storage import load_json, save_json
from telethon import utils as telethon_utils


SOURCES_PATH = PROFILES_DIR / "saved_sources.json"


def normalize_source_ref(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("Source cannot be empty.")
    if value.casefold() in {"saved", "saved messages", "me", "self"}:
        return "me"
    if "t.me/" in value:
        tail = value.split("t.me/", 1)[1].strip("/")
        if tail.startswith("c/"):
            parts = tail.split("/")
            if len(parts) < 2 or not parts[1].isdigit():
                raise ValueError("Invalid private Telegram source link.")
            return f"-100{parts[1]}"
        username = tail.split("/", 1)[0].lstrip("@")
        if not username:
            raise ValueError("Invalid Telegram source link.")
        return f"@{username}"
    if value.startswith("@"):
        return value
    if value.lstrip("-").isdigit():
        return value
    return f"@{value}"


def source_ref_from_dialog(dialog_id: int, peer_type: str) -> str:
    entity_id = abs(int(dialog_id))
    if str(peer_type or "channel").strip().casefold() == "chat":
        return f"-{entity_id}"
    return f"-100{entity_id}"


async def resolve_source_entity(tg_client, source_ref):
    try:
        return await tg_client.get_entity(source_ref)
    except Exception as original:
        if not isinstance(source_ref, int):
            raise
        async for dialog in tg_client.iter_dialogs():
            entity = getattr(dialog, "entity", None)
            if entity is None:
                continue
            try:
                peer_id = int(telethon_utils.get_peer_id(entity))
            except Exception:
                continue
            if peer_id == int(source_ref):
                return entity
        raise original


@dataclass
class SavedSource:
    ref: str
    label: str
    kind: str = "channel"
    added_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SavedSource":
        return cls(
            ref=str(value.get("ref") or "").strip(),
            label=str(value.get("label") or value.get("ref") or "Unknown").strip(),
            kind=str(value.get("kind") or "channel").strip(),
            added_at=str(value.get("added_at") or "").strip(),
        )


def load_sources() -> list[SavedSource]:
    raw = load_json(SOURCES_PATH, default=[])
    if not isinstance(raw, list):
        return []
    sources: list[SavedSource] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = SavedSource.from_dict(item)
        key = source.ref.casefold()
        if not source.ref or key in seen:
            continue
        seen.add(key)
        sources.append(source)
    return sources


def save_sources(sources: list[SavedSource]) -> None:
    save_json(SOURCES_PATH, [asdict(source) for source in sources])


def add_source(*, ref: str, label: str, kind: str) -> tuple[SavedSource, bool]:
    sources = load_sources()
    key = ref.strip().casefold()
    for source in sources:
        if source.ref.casefold() == key:
            source.label = label.strip() or source.label
            source.kind = kind.strip() or source.kind
            save_sources(sources)
            return source, False
    source = SavedSource(
        ref=ref.strip(),
        label=label.strip() or ref.strip(),
        kind=kind.strip() or "channel",
        added_at=datetime.now().isoformat(),
    )
    sources.append(source)
    sources.sort(key=lambda item: item.label.casefold())
    save_sources(sources)
    return source, True


def remove_source(index: int) -> SavedSource | None:
    sources = load_sources()
    if index < 1 or index > len(sources):
        return None
    removed = sources.pop(index - 1)
    save_sources(sources)
    return removed


def remove_source_by_ref(ref: str) -> SavedSource | None:
    sources = load_sources()
    key = str(ref or "").strip().casefold()
    for index, source in enumerate(sources):
        if source.ref.casefold() != key:
            continue
        removed = sources.pop(index)
        save_sources(sources)
        return removed
    return None
