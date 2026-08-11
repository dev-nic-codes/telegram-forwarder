from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import deque
from typing import Deque, Optional, Dict, Any, List

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text


@dataclass
class DashboardState:
    campaign_name: str
    campaign_id: str
    mode: str  # "DRY RUN" or "LIVE"
    started_at: datetime
    sent_total: int = 0
    sent_in_batch: int = 0
    next_at: Optional[datetime] = None
    paused: bool = False
    last_action: str = "-"
    last_target: str = "-"
    last_link: str = "-"
    last_error: str = "-"


class CampaignDashboard:
    def __init__(self, *, console: Console, campaign_name: str, campaign_id: str, mode: str) -> None:
        self.console = console
        self.state = DashboardState(
            campaign_name=campaign_name,
            campaign_id=campaign_id,
            mode=mode,
            started_at=datetime.now(),
        )
        self.events: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._live: Optional[Live] = None

    def start(self) -> None:
        self._live = Live(self._render(), console=self.console, refresh_per_second=8)
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def update_from_event(self, ev: Dict[str, Any]) -> None:
        self.events.appendleft(ev)

        et = ev.get("type", "event")
        if et == "sent":
            self.state.sent_total = int(ev.get("sent_total", self.state.sent_total))
            self.state.sent_in_batch = int(ev.get("sent_in_batch", self.state.sent_in_batch))
            self.state.last_action = "SENT"
            self.state.last_target = str(ev.get("target", "-"))
            self.state.last_link = str(ev.get("link", "-"))
            self.state.last_error = "-"
        elif et == "dry_send":
            self.state.sent_total = int(ev.get("sent_total", self.state.sent_total))
            self.state.sent_in_batch = int(ev.get("sent_in_batch", self.state.sent_in_batch))
            self.state.last_action = "DRY_SEND"
            self.state.last_target = str(ev.get("target", "-"))
            self.state.last_link = str(ev.get("link", "-"))
            self.state.last_error = "-"
        elif et == "error":
            self.state.last_action = "ERROR"
            self.state.last_target = str(ev.get("target", "-"))
            self.state.last_link = str(ev.get("link", "-"))
            self.state.last_error = str(ev.get("error", "-"))
        elif et == "wait":
            self.state.last_action = "WAIT"
        elif et == "paused":
            self.state.paused = True
            self.state.last_action = "PAUSED"
        elif et == "resumed":
            self.state.paused = False
            self.state.last_action = "RESUMED"

        na = ev.get("next_at", None)
        if isinstance(na, datetime) or na is None:
            self.state.next_at = na

        if self._live:
            self._live.update(self._render())

    def _render(self):
        top = self._render_top_panel()
        recent = self._render_recent_table()
        return Group(top, recent)

    def _render_top_panel(self) -> Panel:
        s = self.state

        title = Text()
        title.append("Telegram Forwarder", style="bold")
        title.append("  ")
        title.append(f"[{s.mode}]", style="bold cyan")

        lines: List[str] = []
        lines.append(f"Ad: {s.ad_name}  (id={s.ad_id})")
        lines.append(f"Started:  {s.started_at.strftime('%H:%M:%S %d/%m/%Y')}")
        lines.append(f"Sent:     {s.sent_total}   Batch: {s.sent_in_batch}")
        lines.append(f"Paused:   {'YES' if s.paused else 'NO'}")

        if s.next_at:
            lines.append(f"Next at:  {s.next_at.strftime('%H:%M:%S %d/%m/%Y')}")
        else:
            lines.append("Next at:  -")

        lines.append("")
        lines.append(f"Last:     {s.last_action}")
        lines.append(f"Target:   {s.last_target}")
        lines.append(f"Link:     {s.last_link}")
        if s.last_error and s.last_error != "-":
            lines.append(f"Error:    {s.last_error}")

        body = "\n".join(lines)
        return Panel(body, title=title, border_style="cyan")

    def _render_recent_table(self) -> Table:
        t = Table(title="Recent activity", show_header=True, header_style="bold")
        t.add_column("Time", width=19)
        t.add_column("Type", width=10)
        t.add_column("Target", overflow="fold")
        t.add_column("Link", overflow="fold")
        t.add_column("Info", overflow="fold")

        for ev in list(self.events)[:12]:
            ts: datetime = ev.get("ts") or datetime.now()
            et = str(ev.get("type", "event"))
            target = str(ev.get("target", "-"))
            link = str(ev.get("link", "-"))
            info = str(ev.get("info", ev.get("error", "-")))

            t.add_row(ts.strftime("%H:%M:%S %d/%m/%Y"), et, target, link, info)

        return t
