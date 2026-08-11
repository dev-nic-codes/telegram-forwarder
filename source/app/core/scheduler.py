from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional


@dataclass
class PlannedAction:
    at: datetime
    target_label: str
    message_link: str


class ShuffleBag:
    def __init__(self, items: List[int]) -> None:
        self._items = list(items)
        self._bag: List[int] = []

    def next(self, rng: random.Random) -> int:
        if not self._items:
            raise ValueError("Empty shuffle bag")
        if not self._bag:
            self._bag = list(self._items)
            rng.shuffle(self._bag)
        return self._bag.pop()


class RoundRobin:
    def __init__(self, items: List[int]) -> None:
        self._items = list(items)
        self._idx = 0

    def next(self) -> int:
        if not self._items:
            raise ValueError("Empty round robin")
        v = self._items[self._idx]
        self._idx = (self._idx + 1) % len(self._items)
        return v


def _rand_between(rng: random.Random, a: int, b: int) -> int:
    lo = min(a, b)
    hi = max(a, b)
    return int(rng.randint(lo, hi))


def plan_actions(
    *,
    start_at: Optional[datetime],
    message_links: List[str],
    targets: List[dict],
    send_gap_min_sec: int,
    send_gap_max_sec: int,
    batch_gap_min_sec: int,
    batch_gap_max_sec: int,
    message_strategy: str,
    target_strategy: str,
    steps: int = 30,
    seed: Optional[int] = None,
) -> List[PlannedAction]:
    """
    Simulation only. Plans 'steps' sends.

    Batch definition:
    - One batch is one full pass where each target is picked once.
    - After finishing all targets, we wait a batch gap, then reshuffle and continue.
    """

    if not message_links:
        raise ValueError("No message links provided")
    if not targets:
        raise ValueError("No targets provided")
    if steps <= 0:
        return []

    rng = random.Random(seed)

    msg_idxs = list(range(len(message_links)))
    tgt_idxs = list(range(len(targets)))

    if message_strategy == "shuffle_bag":
        msg_picker = ShuffleBag(msg_idxs)
        msg_rr = None
    elif message_strategy == "round_robin":
        msg_picker = None
        msg_rr = RoundRobin(msg_idxs)
    else:
        raise ValueError("Unknown message strategy")

    if target_strategy == "shuffle_bag":
        tgt_picker = ShuffleBag(tgt_idxs)
        tgt_rr = None
    elif target_strategy == "round_robin":
        tgt_picker = None
        tgt_rr = RoundRobin(tgt_idxs)
    else:
        raise ValueError("Unknown target strategy")

    now = start_at or datetime.now()
    planned: List[PlannedAction] = []

    targets_in_batch = len(targets)
    sent_in_current_batch = 0

    for _ in range(steps):
        if tgt_picker is not None:
            ti = tgt_picker.next(rng)
        else:
            ti = tgt_rr.next()  # type: ignore[union-attr]

        if msg_picker is not None:
            mi = msg_picker.next(rng)
        else:
            mi = msg_rr.next()  # type: ignore[union-attr]

        t = targets[ti]
        group_title = str(t.get("group_title", "Unknown"))
        topic_title = t.get("topic_title", None)
        if topic_title:
            target_label = f"{group_title} -> {topic_title}"
        else:
            target_label = f"{group_title} (no topic)"

        planned.append(
            PlannedAction(
                at=now,
                target_label=target_label,
                message_link=message_links[mi],
            )
        )

        sent_in_current_batch += 1

        send_gap = _rand_between(rng, send_gap_min_sec, send_gap_max_sec)
        now = now + timedelta(seconds=send_gap)

        if sent_in_current_batch >= targets_in_batch:
            batch_gap = _rand_between(rng, batch_gap_min_sec, batch_gap_max_sec)
            now = now + timedelta(seconds=batch_gap)
            sent_in_current_batch = 0

    return planned
