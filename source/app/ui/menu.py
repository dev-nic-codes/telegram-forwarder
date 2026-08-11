# app/ui/menu.py

from __future__ import annotations

from typing import List

from rich.console import Console
from rich.panel import Panel


from app.core.destinations import Destination

console = Console()

APP_VERSION = "v2.0.0"


def print_header() -> None:
    console.print()
    console.print("[bold cyan]Telegram Forwarder[/bold cyan]")
    console.print("[dim]Message forwarding and scheduling[/dim]")
    console.print()


def main_menu() -> str:
    """Display beautiful sectioned menu"""
    console.print()

    # LOGIN SECTION
    login_panel = Panel(
        "[bold]1.[/bold] First-time setup (API credentials)\n"
        "[bold]2.[/bold] Login to Telegram\n"
        "[bold]3.[/bold] Logout",
        title="[bold cyan]🔐 LOGIN[/bold cyan]",
        border_style="cyan",
        padding=(0, 2)
    )
    console.print(login_panel)

    # GROUPS SECTION
    groups_panel = Panel(
        "[bold]4.[/bold] Sync destinations (scan groups)\n"
        "[bold]5.[/bold] View forum topics\n"
        "[bold]6.[/bold] Configure forwarding destinations\n"
        "[bold]7.[/bold] View saved destinations\n"
        "[bold]8.[/bold] Edit/delete destinations",
        title="[bold green]📱 GROUPS & DESTINATIONS[/bold green]",
        border_style="green",
        padding=(0, 2)
    )
    console.print(groups_panel)

    # ADS SECTION
    ads_panel = Panel(
        "[bold]9.[/bold] Create new ad\n"
        "[bold]10.[/bold] View all ads\n"
        "[bold]11.[/bold] Edit ad\n"
        "[bold]12.[/bold] Run ad (TEST MODE - no sending)\n"
        "[bold]13.[/bold] Run ad (LIVE MODE)\n"
        "[bold]14.[/bold] Ad status & statistics\n"
        "[bold]15.[/bold] Pause ad\n"
        "[bold]16.[/bold] Resume ad\n"
        "[bold]17.[/bold] Stop ad",
        title="[bold magenta]🚀 ADS[/bold magenta]",
        border_style="magenta",
        padding=(0, 2)
    )
    console.print(ads_panel)

    # ANALYTICS SECTION
    analytics_panel = Panel(
        "[bold]18.[/bold] View message history\n"
        "[bold]19.[/bold] Ad statistics\n"
        "[bold]20.[/bold] Message-Group matrix\n"
        "[bold]21.[/bold] Export history to CSV\n"
        "[bold]22.[/bold] Total messages sent",
        title="[bold yellow]📊 ANALYTICS & HISTORY[/bold yellow]",
        border_style="yellow",
        padding=(0, 2)
    )
    console.print(analytics_panel)

    # TELEGRAM BOT SECTION
    bot_panel = Panel(
        "[bold]23.[/bold] Setup Telegram bot\n"
        "[bold]24.[/bold] Bot settings\n"
        "[bold]25.[/bold] Test bot connection",
        title="[bold blue]🤖 TELEGRAM BOT SETTINGS[/bold blue]",
        border_style="blue",
        padding=(0, 2)
    )
    console.print(bot_panel)

    # SETTINGS SECTION
    settings_panel = Panel(
        "[bold]26.[/bold] Import destinations & ads\n"
        "[bold]27.[/bold] Export destinations & ads\n"
        "[bold]28.[/bold] Delete destinations\n"
        "[bold]29.[/bold] Delete ads\n"
        "[bold]30.[/bold] Delete session (force re-login)\n"
        "[bold]31.[/bold] Delete all data (RESET)\n"
        "[bold]32.[/bold] Advanced settings",
        title="[bold red]⚙️  SETTINGS & DATA[/bold red]",
        border_style="red",
        padding=(0, 2)
    )
    console.print(settings_panel)

    # ACCOUNTS SECTION
    accounts_panel = Panel(
        "[bold]33.[/bold] Manage accounts (multi-account)",
        title="[bold cyan]👤 ACCOUNTS[/bold cyan]",
        border_style="cyan",
        padding=(0, 2)
    )
    console.print(accounts_panel)

    # RUNNING SECTION
    running_panel = Panel(
        "[bold]34.[/bold] View running ads\n"
        "[bold]35.[/bold] Stop running ad\n"
        "[bold]36.[/bold] Toggle live updates\n"
        "[bold]37.[/bold] Live updates panel",
        title="[bold magenta]🟢 RUNNING[/bold magenta]",
        border_style="magenta",
        padding=(0, 2)
    )
    console.print(running_panel)

    # EXIT
    console.print()
    console.print("[bold]0.[/bold] [red]Exit[/red]")
    console.print()

    return input("Select option: ").strip()


def render_destinations(destinations: List[Destination], limit: int = 60) -> None:
    """Display destinations in a nice format"""
    from rich.table import Table

    table = Table(title=" Destinations", show_header=True, header_style="bold magenta")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Status")
    table.add_column("Username", style="dim")
    table.add_column("Topics", justify="center")
    table.add_column("Stars", justify="center")

    show = destinations[:limit]
    for i, d in enumerate(show, start=1):
        status_icon = "" if d.status == "likely" else ("" if d.status == "unknown" else "")
        status_text = "Postable" if d.status == "likely" else ("Needs admin" if d.status == "unknown" else "Blocked")
        topics_flag = "" if getattr(d, "is_forum", False) else "-"
        stars_val = getattr(d, "paid_message_stars", None)
        stars_txt = f" {stars_val}" if isinstance(stars_val, int) and stars_val > 0 else "-"

        table.add_row(
            str(i),
            d.title,
            d.kind,
            f"{status_icon} {status_text}",
            d.username or "-",
            topics_flag,
            stars_txt,
        )

    console.print(table)
    if len(destinations) > limit:
        console.print(f"[dim]Showing {limit} of {len(destinations)} destinations.[/dim]")
