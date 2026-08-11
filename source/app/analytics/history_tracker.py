# app/analytics/history_tracker.py
"""Message history tracking and viewing"""
from __future__ import annotations

import aiosqlite
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path


class MessageHistory:
    """Track and query message send history"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Connect to database"""
        self.conn = await aiosqlite.connect(str(self.db_path))
        self.conn.row_factory = aiosqlite.Row
        await self._init_tables()

    async def close(self):
        """Close connection"""
        if self.conn:
            await self.conn.close()

    async def _init_tables(self):
        """Initialize history tables"""
        if not self.conn:
            return

        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS send_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                message_link TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                group_title TEXT NOT NULL,
                topic_id INTEGER,
                topic_title TEXT,
                success INTEGER NOT NULL,
                error_type TEXT,
                error_message TEXT,
                stars_cost INTEGER DEFAULT 0
            )
        ''')

        await self.conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_history_campaign
            ON send_history(campaign_id)
        ''')

        await self.conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_history_group
            ON send_history(group_id)
        ''')

        await self.conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_history_timestamp
            ON send_history(timestamp)
        ''')

        await self.conn.commit()

    async def log_send(
        self,
        campaign_id: str,
        campaign_name: str,
        message_link: str,
        group_id: int,
        group_title: str,
        topic_id: Optional[int],
        topic_title: Optional[str],
        success: bool,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        stars_cost: int = 0
    ):
        """Log a message send"""
        if not self.conn:
            return

        await self.conn.execute('''
            INSERT INTO send_history (
                timestamp, campaign_id, campaign_name, message_link,
                group_id, group_title, topic_id, topic_title,
                success, error_type, error_message, stars_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(),
            campaign_id,
            campaign_name,
            message_link,
            group_id,
            group_title,
            topic_id,
            topic_title,
            1 if success else 0,
            error_type,
            error_message,
            stars_cost
        ))
        await self.conn.commit()

    async def get_recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent send history"""
        if not self.conn:
            return []

        cursor = await self.conn.execute('''
            SELECT * FROM send_history
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_filtered(
        self,
        *,
        limit: int = 100,
        campaign_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent send history with optional filters."""
        if not self.conn:
            return []

        where = []
        params: list[Any] = []
        if campaign_id:
            where.append("campaign_id = ?")
            params.append(campaign_id)
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if until:
            where.append("timestamp <= ?")
            params.append(until)

        sql = "SELECT * FROM send_history"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self.conn.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_recent_errors(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent failed sends"""
        if not self.conn:
            return []

        cursor = await self.conn.execute('''
            SELECT * FROM send_history
            WHERE success = 0
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (limit,))

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_by_campaign(self, campaign_id: str) -> List[Dict[str, Any]]:
        """Get history for specific campaign"""
        if not self.conn:
            return []

        cursor = await self.conn.execute('''
            SELECT * FROM send_history
            WHERE campaign_id = ?
            ORDER BY timestamp DESC
        ''', (campaign_id,))

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_by_group(self, group_id: int) -> List[Dict[str, Any]]:
        """Get history for specific group"""
        if not self.conn:
            return []

        cursor = await self.conn.execute('''
            SELECT * FROM send_history
            WHERE group_id = ?
            ORDER BY timestamp DESC
        ''', (group_id,))

        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """Get statistics for last N days"""
        if not self.conn:
            return {}

        since = (datetime.now() - timedelta(days=days)).isoformat()

        cursor = await self.conn.execute('''
            SELECT
                COUNT(*) as total,
                SUM(success) as successful,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                SUM(stars_cost) as stars_spent
            FROM send_history
            WHERE timestamp >= ?
        ''', (since,))

        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def get_campaign_stats(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get per-campaign stats for last N days."""
        if not self.conn:
            return []

        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            '''
            SELECT
                campaign_id,
                campaign_name,
                COUNT(*) as total,
                SUM(success) as successful,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                MAX(timestamp) as last_sent
            FROM send_history
            WHERE timestamp >= ?
            GROUP BY campaign_id, campaign_name
            ORDER BY total DESC
            ''',
            (since,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_error_breakdown(self, days: int = 7) -> List[Dict[str, Any]]:
        """Get error breakdown for last N days."""
        if not self.conn:
            return []

        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            '''
            SELECT
                error_type,
                COUNT(*) as count
            FROM send_history
            WHERE success = 0 AND timestamp >= ?
            GROUP BY error_type
            ORDER BY count DESC
            ''',
            (since,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_group_failures(self, days: int = 7, limit: int = 10) -> List[Dict[str, Any]]:
        """Get groups with most failures for last N days."""
        if not self.conn:
            return []

        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            '''
            SELECT
                group_id,
                group_title,
                COUNT(*) as total,
                SUM(success) as successful,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                MAX(timestamp) as last_sent
            FROM send_history
            WHERE timestamp >= ?
            GROUP BY group_id, group_title
            ORDER BY failed DESC, total DESC
            LIMIT ?
            ''',
            (since, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_totals(self) -> Dict[str, Any]:
        """Get total statistics for all time"""
        if not self.conn:
            return {}

        cursor = await self.conn.execute('''
            SELECT
                COUNT(*) as total,
                SUM(success) as successful,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed,
                SUM(stars_cost) as stars_spent
            FROM send_history
        ''')

        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def cleanup_older_than(self, days: int) -> int:
        """Delete history older than N days. Returns rows deleted."""
        if not self.conn:
            return 0
        try:
            since = (datetime.now() - timedelta(days=days)).isoformat()
            cur = await self.conn.execute(
                "DELETE FROM send_history WHERE timestamp < ?",
                (since,),
            )
            await self.conn.commit()
            return int(cur.rowcount or 0)
        except Exception:
            return 0

    async def get_group_matrix(self) -> Dict[str, Any]:
        """Get message-group send matrix"""
        if not self.conn:
            return {}

        cursor = await self.conn.execute('''
            SELECT
                group_title,
                message_link,
                MAX(timestamp) as last_sent,
                SUM(success) as times_sent
            FROM send_history
            GROUP BY group_id, message_link
            ORDER BY group_title, message_link
        ''')

        rows = await cursor.fetchall()

        # Organize into matrix format
        matrix = {}
        for row in rows:
            group = row['group_title']
            if group not in matrix:
                matrix[group] = []
            matrix[group].append({
                'message': row['message_link'],
                'last_sent': row['last_sent'],
                'times_sent': row['times_sent']
            })

        return matrix


# Global instance
_history: Optional[MessageHistory] = None


async def get_history() -> MessageHistory:
    """Get or create global history instance"""
    global _history
    if _history is None:
        from app.utils.paths import DATABASE_FILE
        _history = MessageHistory(DATABASE_FILE)
        await _history.connect()
    return _history


async def close_history():
    """Close global history instance"""
    global _history
    if _history:
        await _history.close()
        _history = None
