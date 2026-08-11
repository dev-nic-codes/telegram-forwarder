# app/menu_handlers.py
"""Menu handlers"""
from __future__ import annotations


from datetime import datetime, timedelta
from typing import Optional

from rich.table import Table


from app.analytics.history_tracker import get_history
from app.ui.menu import console
from app.utils.paths import EXPORTS_DIR


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S %d/%m/%Y")
    except Exception:
        return (ts or "")[:19]


def _friendly_error(name: str) -> str:
    key = (name or "").strip()
    mapping = {
        "SlowModeWaitError": "Slow mode (wait required)",
        "ConnectionError": "Connection issue",
        "OperationalError": "Database locked",
        "ValueError": "Invalid data",
        "ForbiddenError": "No permission",
    }
    return mapping.get(key, key or "Error")


async def view_message_history(limit: int = 50):
    """View recent message send history (optional filters)"""
    console.print("\n[bold cyan] MESSAGE HISTORY [/bold cyan]\n")

    try:
        history = await get_history()
        raw_days = input("Filter by last N days (blank = all): ").strip()
        raw_ad = input("Filter by ad id (blank = all): ").strip()
        since = None
        if raw_days.isdigit() and int(raw_days) > 0:
            since = (datetime.now() - timedelta(days=int(raw_days))).isoformat()
        records = await history.get_recent_filtered(
            limit=limit,
            campaign_id=raw_ad or None,
            since=since,
        )

        if not records:
            console.print("[yellow]No message history yet. History is logged when you run ads.[/yellow]")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Time", style="dim")
        table.add_column("Ad")
        table.add_column("Group")
        table.add_column("Topic", style="dim")
        table.add_column("Status")

        for record in records[:limit]:
            timestamp = _fmt_ts(record.get("timestamp"))
            ad = str(record.get("campaign_name", "Unknown"))[:25]
            group = str(record.get("group_title", "Unknown"))[:30]
            topic = record.get('topic_title', '-')[:20] if record.get('topic_title') else '-'

            if record['success']:
                status = "[green] Sent[/green]"
            else:
                error_type = _friendly_error(record.get('error_type', 'Error'))
                status = f"[red] {error_type}[/red]"

            table.add_row(timestamp, ad, group, topic, status)

        console.print(table)
        console.print(f"\n[dim]Showing last {len(records)} records[/dim]")

    except Exception as e:
        console.print(f"[red]Error loading history: {e}[/red]")


async def view_ad_stats():
    """View ad statistics"""
    console.print("\n[bold cyan] CAMPAIGN STATISTICS [/bold cyan]\n")

    try:
        history = await get_history()

        # Get stats for different time periods
        stats_7d = await history.get_stats(days=7)
        stats_30d = await history.get_stats(days=30)

        # Last 7 days (overall)
        console.print("[bold] Last 7 Days[/bold]")
        if stats_7d and stats_7d.get('total', 0) > 0:
            total = stats_7d['total']
            successful = stats_7d['successful']
            failed = stats_7d['failed']
            success_rate = (successful / total * 100) if total > 0 else 0

            console.print(f"  Total sends: {total}")
            console.print(f"  [green] Successful: {successful}[/green]")
            console.print(f"  [red] Failed: {failed}[/red]")
            console.print(f"  Success rate: {success_rate:.1f}%")
        else:
            console.print("  [dim]No activity in last 7 days[/dim]")

        console.print()

        # Last 30 days (overall)
        console.print("[bold] Last 30 Days[/bold]")
        if stats_30d and stats_30d.get('total', 0) > 0:
            total = stats_30d['total']
            successful = stats_30d['successful']
            failed = stats_30d['failed']
            success_rate = (successful / total * 100) if total > 0 else 0

            console.print(f"  Total sends: {total}")
            console.print(f"  [green] Successful: {successful}[/green]")
            console.print(f"  [red] Failed: {failed}[/red]")
            console.print(f"  Success rate: {success_rate:.1f}%")
        else:
            console.print("  [dim]No activity in last 30 days[/dim]")

        # Per-campaign summary
        console.print("\n[bold] Per-Ad (Last 7 Days)[/bold]")
        campaigns = await history.get_campaign_stats(days=7)
        if not campaigns:
            console.print("  [dim]No ad activity in last 7 days[/dim]")
        else:
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Ad")
            table.add_column("Total", justify="right")
            table.add_column("Success", justify="right")
            table.add_column("Failed", justify="right")
            table.add_column("Last Sent", style="dim")
            for c in campaigns[:10]:
                total = int(c.get("total") or 0)
                succ = int(c.get("successful") or 0)
                failed = int(c.get("failed") or 0)
                last_sent = _fmt_ts(c.get("last_sent"))
                name = c.get("campaign_name", "...")
                table.add_row(name, str(total), str(succ), str(failed), last_sent)
            console.print(table)

        # Error breakdown
        console.print("\n[bold] Error Breakdown (Last 7 Days)[/bold]")
        errors = await history.get_error_breakdown(days=7)
        if not errors:
            console.print("  [dim]No errors in last 7 days[/dim]")
        else:
            for e in errors[:10]:
                et = _friendly_error(e.get("error_type") or "Error")
                cnt = int(e.get("count") or 0)
                console.print(f"  - {et}: {cnt}")

        # Top failing groups
        console.print("\n[bold] Top Failing Groups (Last 7 Days)[/bold]")
        groups = await history.get_group_failures(days=7, limit=10)
        if not groups:
            console.print("  [dim]No group failures in last 7 days[/dim]")
        else:
            table = Table(show_header=True, header_style="bold magenta")
            table.add_column("Group")
            table.add_column("Failed", justify="right")
            table.add_column("Total", justify="right")
            table.add_column("Last Sent", style="dim")
            for g in groups:
                total = int(g.get("total") or 0)
                failed = int(g.get("failed") or 0)
                last_sent = _fmt_ts(g.get("last_sent"))
                title = g.get("group_title", "...")[:30]
                table.add_row(title, str(failed), str(total), last_sent)
            console.print(table)

    except Exception as e:
        console.print(f"[red]Error loading stats: {e}[/red]")


async def view_group_matrix():
    """View message-group send matrix"""
    console.print("\n[bold cyan] MESSAGE-GROUP MATRIX [/bold cyan]\n")

    try:
        history = await get_history()
        matrix = await history.get_group_matrix()

        if not matrix:
            console.print("[yellow]No message history yet.[/yellow]")
            return

        for group_title, messages in list(matrix.items())[:20]:  # Limit to 20 groups
            console.print(f"\n[bold]{group_title}[/bold]")
            for msg in messages[:5]:  # Limit to 5 messages per group
                msg_short = msg['message'][-50:] if len(msg['message']) > 50 else msg['message']
                last_sent = _fmt_ts(msg.get("last_sent")) if msg.get("last_sent") else "Never"
                times = msg['times_sent']
                console.print(f"   {msg_short}")
                console.print(f"    Last sent: {last_sent} | Times sent: {times}")

        console.print(f"\n[dim]Showing first 20 groups. Full matrix has {len(matrix)} groups.[/dim]")

    except Exception as e:
        console.print(f"[red]Error loading matrix: {e}[/red]")


async def export_history_csv():
    """Export message history to CSV"""
    console.print("\n[bold cyan] EXPORT HISTORY [/bold cyan]\n")

    try:
        history = await get_history()
        records = await history.get_recent(limit=1000)

        if not records:
            console.print("[yellow]No history to export.[/yellow]")
            return

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"message_history_{timestamp}.csv"
        filepath = EXPORTS_DIR / filename

        # Write CSV
        with open(filepath, 'w', encoding='utf-8') as f:
            # Header
            f.write("Timestamp,Ad ID,Ad Name,Message Link,Group ID,Group Title,Topic ID,Topic Title,Success,Error Type,Error Message\n")

            # Data
            for r in records:
                f.write(f'"{r["timestamp"]}",')
                f.write(f'"{r["campaign_id"]}",')
                f.write(f'"{r["campaign_name"]}",')
                f.write(f'"{r["message_link"]}",')
                f.write(f'"{r["group_id"]}",')
                f.write(f'"{r["group_title"]}",')
                f.write(f'"{r.get("topic_id", "")}",')
                f.write(f'"{r.get("topic_title", "")}",')
                f.write(f'"{r["success"]}",')
                f.write(f'"{r.get("error_type", "")}",')
                f.write(f'"{r.get("error_message", "")}",')
                f.write("\n")

        console.print(f"[green] Exported {len(records)} records to:[/green]")
        console.print(f"[cyan]{filepath}[/cyan]")

    except Exception as e:
        console.print(f"[red]Error exporting: {e}[/red]")


async def view_total_messages():
    """View total messages sent"""
    console.print("\n[bold cyan]=== TOTAL MESSAGES SENT ===[/bold cyan]\n")

    try:
        history = await get_history()
        totals = await history.get_totals()

        total = int(totals.get("total") or 0)
        successful = int(totals.get("successful") or 0)
        failed = int(totals.get("failed") or 0)
        success_rate = (successful / total * 100) if total > 0 else 0.0

        console.print(f"Total messages: {total}")
        console.print(f"[green]Successful: {successful}[/green]")
        console.print(f"[red]Failed: {failed}[/red]")
        console.print(f"Success rate: {success_rate:.1f}%")

    except Exception as e:
        console.print(f"[red]Error loading totals: {e}[/red]")
