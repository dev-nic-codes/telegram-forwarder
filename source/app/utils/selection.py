from __future__ import annotations

from typing import List, Set


def parse_selection(text: str, max_index: int) -> List[int]:
    """
    Parse input like: "1,3,5-8" or "all"
    Returns sorted unique indices in range [1..max_index]
    """
    s = text.strip().lower()
    if not s:
        return []
    if s == "all":
        return list(range(1, max_index + 1))

    chosen: Set[int] = set()
    parts = [p.strip() for p in s.split(",") if p.strip()]
    for p in parts:
        if "-" in p:
            a, b = p.split("-", 1)
            if not a.strip().isdigit() or not b.strip().isdigit():
                continue
            start = int(a.strip())
            end = int(b.strip())
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                if 1 <= i <= max_index:
                    chosen.add(i)
        else:
            if not p.isdigit():
                continue
            i = int(p)
            if 1 <= i <= max_index:
                chosen.add(i)

    return sorted(chosen)
