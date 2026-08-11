from __future__ import annotations

from datetime import datetime, time, timedelta, date
from typing import Dict, List, Optional


def _parse_hhmm(value: Optional[str]) -> Optional[time]:
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except Exception:
        return None
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return time(hour=h, minute=m)


def _is_day_allowed(d: date, mode: str) -> bool:
    mode_l = (mode or "all").strip().lower()
    wd = d.weekday()  # 0=Mon .. 6=Sun
    if mode_l == "weekday":
        return wd <= 4
    if mode_l == "weekend":
        return wd >= 5
    return True


def _normalize_windows(windows: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for w in windows or []:
        if not isinstance(w, dict):
            continue
        start = w.get("start")
        end = w.get("end")
        if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
            continue
        out.append({"start": start, "end": end, "label": str(w.get("label", "")).strip()})
    return out


def _in_time_window(now_t: time, start_t: time, end_t: time) -> bool:
    if start_t == end_t:
        return True
    if end_t > start_t:
        return start_t <= now_t < end_t
    # overnight window
    return now_t >= start_t or now_t < end_t


def _sleep_end(now: datetime, sleep_start: time, sleep_end: time) -> datetime:
    if sleep_start == sleep_end:
        return now
    if sleep_end > sleep_start:
        end_dt = datetime.combine(now.date(), sleep_end)
        if now <= end_dt:
            return end_dt
        return datetime.combine(now.date() + timedelta(days=1), sleep_end)
    # overnight sleep
    if now.time() >= sleep_start:
        return datetime.combine(now.date() + timedelta(days=1), sleep_end)
    return datetime.combine(now.date(), sleep_end)


def is_sleep_time(now: datetime, sleep_start: str, sleep_end: str) -> bool:
    st = _parse_hhmm(sleep_start)
    en = _parse_hhmm(sleep_end)
    if st is None or en is None:
        return False
    return _in_time_window(now.time(), st, en)


def _active_window_end(
    now: datetime,
    *,
    days_mode: str,
    windows: List[Dict[str, str]],
) -> Optional[datetime]:
    for day_offset in (0, -1):
        day = now.date() + timedelta(days=day_offset)
        if not _is_day_allowed(day, days_mode):
            continue
        for w in windows:
            st = _parse_hhmm(w["start"])
            en = _parse_hhmm(w["end"])
            if st is None or en is None:
                continue
            start_dt = datetime.combine(day, st)
            end_dt = datetime.combine(day, en)
            if end_dt <= start_dt:
                end_dt = end_dt + timedelta(days=1)
            if start_dt <= now < end_dt:
                return end_dt
    return None


def _windows_for_day(
    d: date,
    *,
    windows: Optional[List[Dict[str, str]]],
    windows_weekday: Optional[List[Dict[str, str]]],
    windows_weekend: Optional[List[Dict[str, str]]],
) -> Optional[List[Dict[str, str]]]:
    if d.weekday() <= 4:
        return windows_weekday if windows_weekday is not None else windows
    return windows_weekend if windows_weekend is not None else windows


def is_allowed_now(
    now: datetime,
    *,
    days_mode: str,
    windows: Optional[List[Dict[str, str]]],
    sleep_start: str,
    sleep_end: str,
    windows_weekday: Optional[List[Dict[str, str]]] = None,
    windows_weekend: Optional[List[Dict[str, str]]] = None,
) -> bool:
    if is_sleep_time(now, sleep_start, sleep_end):
        return False
    win = _windows_for_day(
        now.date(),
        windows=windows,
        windows_weekday=windows_weekday,
        windows_weekend=windows_weekend,
    )
    if win:
        active_end = _active_window_end(now, days_mode=days_mode, windows=_normalize_windows(win))
        return active_end is not None
    return _is_day_allowed(now.date(), days_mode)


def next_allowed_time(
    now: datetime,
    *,
    days_mode: str,
    windows: Optional[List[Dict[str, str]]],
    sleep_start: str,
    sleep_end: str,
    windows_weekday: Optional[List[Dict[str, str]]] = None,
    windows_weekend: Optional[List[Dict[str, str]]] = None,
) -> datetime:
    if is_allowed_now(
        now,
        days_mode=days_mode,
        windows=windows,
        sleep_start=sleep_start,
        sleep_end=sleep_end,
        windows_weekday=windows_weekday,
        windows_weekend=windows_weekend,
    ):
        return now

    if is_sleep_time(now, sleep_start, sleep_end):
        return _sleep_end(now, _parse_hhmm(sleep_start) or time(0, 0), _parse_hhmm(sleep_end) or time(0, 0))

    # no windows: next allowed day start (if day filter blocks)
    if not _windows_for_day(
        now.date(),
        windows=windows,
        windows_weekday=windows_weekday,
        windows_weekend=windows_weekend,
    ):
        if _is_day_allowed(now.date(), days_mode):
            return now
        for offset in range(1, 8):
            day = now.date() + timedelta(days=offset)
            if _is_day_allowed(day, days_mode):
                return datetime.combine(day, time(0, 0))
        return now

    # search for next window start
    for offset in range(0, 8):
        day = now.date() + timedelta(days=offset)
        if not _is_day_allowed(day, days_mode):
            continue
        win = _windows_for_day(
            day,
            windows=windows,
            windows_weekday=windows_weekday,
            windows_weekend=windows_weekend,
        )
        win = _normalize_windows(win)
        for w in win:
            st = _parse_hhmm(w["start"])
            en = _parse_hhmm(w["end"])
            if st is None or en is None:
                continue
            start_dt = datetime.combine(day, st)
            end_dt = datetime.combine(day, en)
            if end_dt <= start_dt:
                end_dt = end_dt + timedelta(days=1)
            if end_dt <= now:
                continue
            cand = start_dt if start_dt >= now else now
            if cand > end_dt:
                continue
            if is_sleep_time(cand, sleep_start, sleep_end):
                cand = _sleep_end(
                    cand,
                    _parse_hhmm(sleep_start) or time(0, 0),
                    _parse_hhmm(sleep_end) or time(0, 0),
                )
            if cand <= end_dt and is_allowed_now(
                cand,
                days_mode=days_mode,
                windows=win,
                sleep_start=sleep_start,
                sleep_end=sleep_end,
            ):
                return cand

    return now + timedelta(minutes=5)
