from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from app.utils.paths import PROFILES_DIR
from app.utils.storage import load_json, save_json

TARGETS_PATH = PROFILES_DIR / "destination_targets.json"


@dataclass
class DestinationTarget:
    group_id: int
    group_title: str
    peer_type: str = "channel"
    topic_id: Optional[int] = None
    topic_title: Optional[str] = None
    topic_top_message: Optional[int] = None

    # New fields (needed by your current main.py)
    paid_message_stars: Optional[int] = None
    is_paid: bool = False
    extra_delay_sec: Optional[int] = None

    def key(self) -> Tuple[int, Optional[int]]:
        return (int(self.group_id), int(self.topic_id) if self.topic_id is not None else None)


def load_targets() -> List[DestinationTarget]:
    raw = load_json(TARGETS_PATH, default=[])
    targets: List[DestinationTarget] = []

    for t in raw:
        try:
            group_id = int(t["group_id"])
        except Exception:
            continue

        group_title = str(t.get("group_title", ""))
        peer_type = str(t.get("peer_type") or "channel").strip().casefold()
        if peer_type not in {"channel", "chat"}:
            peer_type = "channel"

        topic_id_raw = t.get("topic_id", None)
        topic_id = int(topic_id_raw) if topic_id_raw is not None else None

        topic_title_raw = t.get("topic_title", None)
        topic_title = str(topic_title_raw) if topic_title_raw is not None else None

        top_msg_raw = t.get("topic_top_message", None)
        topic_top_message = int(top_msg_raw) if top_msg_raw is not None else None

        paid_raw = t.get("paid_message_stars", None)
        paid_message_stars = int(paid_raw) if paid_raw is not None else None

        is_paid_raw = t.get("is_paid", None)
        is_paid = bool(is_paid_raw) if is_paid_raw is not None else False
        delay_raw = t.get("extra_delay_sec", None)
        try:
            extra_delay_sec = int(delay_raw) if delay_raw is not None else None
        except Exception:
            extra_delay_sec = None

        targets.append(
            DestinationTarget(
                group_id=group_id,
                group_title=group_title,
                peer_type=peer_type,
                topic_id=topic_id,
                topic_title=topic_title,
                topic_top_message=topic_top_message,
                paid_message_stars=paid_message_stars,
                is_paid=is_paid,
                extra_delay_sec=extra_delay_sec,
            )
        )

    return targets


def save_targets(targets: List[DestinationTarget]) -> None:
    data = []
    for t in targets:
        data.append(
            {
                "group_id": int(t.group_id),
                "group_title": str(t.group_title),
                "peer_type": str(getattr(t, "peer_type", "channel") or "channel"),
                "topic_id": (int(t.topic_id) if t.topic_id is not None else None),
                "topic_title": (str(t.topic_title) if t.topic_title is not None else None),
                "topic_top_message": (int(t.topic_top_message) if t.topic_top_message is not None else None),

                # New fields
                "paid_message_stars": (int(t.paid_message_stars) if t.paid_message_stars is not None else None),
                "is_paid": bool(getattr(t, "is_paid", False)),
                "extra_delay_sec": (int(t.extra_delay_sec) if t.extra_delay_sec is not None else None),
            }
        )
    save_json(TARGETS_PATH, data)


def add_targets(existing: List[DestinationTarget], new_targets: List[DestinationTarget]) -> List[DestinationTarget]:
    seen: Set[Tuple[int, Optional[int]]] = set(t.key() for t in existing)
    merged = list(existing)
    for t in new_targets:
        k = t.key()
        if k in seen:
            continue
        merged.append(t)
        seen.add(k)
    return merged


def remove_targets(existing: List[DestinationTarget], idxs_1based: List[int]) -> List[DestinationTarget]:
    if not existing:
        return []

    kill = set()
    for i in idxs_1based:
        if isinstance(i, int) and i >= 1:
            kill.add(i - 1)

    out: List[DestinationTarget] = []
    for i, t in enumerate(existing):
        if i not in kill:
            out.append(t)
    return out


def clear_targets_for_group(existing: List[DestinationTarget], group_id: int) -> List[DestinationTarget]:
    if not existing:
        return []

    out: List[DestinationTarget] = []
    for t in existing:
        if int(t.group_id) != int(group_id):
            out.append(t)
    return out
