# app/database/schema.py
import aiosqlite
from pathlib import Path
from typing import Optional
from datetime import datetime


class Database:
    """Central database for all analytics and tracking."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Connect to database."""
        self.conn = await aiosqlite.connect(str(self.db_path))
        self.conn.row_factory = aiosqlite.Row
        await self.init_schema()

    async def close(self):
        """Close database connection."""
        if self.conn:
            await self.conn.close()

    async def init_schema(self):
        """Initialize database schema."""
        if not self.conn:
            return

        # Message send history
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS message_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                campaign_id TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                account_id TEXT NOT NULL,
                message_link TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                group_title TEXT NOT NULL,
                topic_id INTEGER,
                topic_title TEXT,
                success INTEGER NOT NULL,
                error_type TEXT,
                error_message TEXT,
                send_duration_ms INTEGER,
                stars_cost INTEGER DEFAULT 0
            )
        ''')

        # Campaign runs
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS campaign_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                account_id TEXT NOT NULL,
                start_time DATETIME NOT NULL,
                end_time DATETIME,
                total_scheduled INTEGER NOT NULL,
                total_sent INTEGER DEFAULT 0,
                total_failed INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                stars_spent INTEGER DEFAULT 0
            )
        ''')

        # Group health tracking
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS group_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL UNIQUE,
                group_title TEXT NOT NULL,
                last_check DATETIME NOT NULL,
                is_member INTEGER NOT NULL,
                can_post INTEGER NOT NULL,
                is_paid INTEGER DEFAULT 0,
                stars_per_post INTEGER,
                member_count INTEGER,
                success_count INTEGER DEFAULT 0,
                failure_count INTEGER DEFAULT 0,
                last_success DATETIME,
                last_failure DATETIME,
                health_score INTEGER DEFAULT 100,
                notes TEXT
            )
        ''')

        # Account health tracking
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS account_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL UNIQUE,
                phone_number TEXT NOT NULL,
                account_age_days INTEGER,
                last_check DATETIME NOT NULL,
                messages_sent_today INTEGER DEFAULT 0,
                messages_sent_this_hour INTEGER DEFAULT 0,
                last_floodwait DATETIME,
                floodwait_count_30d INTEGER DEFAULT 0,
                kick_count_30d INTEGER DEFAULT 0,
                success_rate_7d REAL DEFAULT 100.0,
                health_score INTEGER DEFAULT 100,
                daily_cap INTEGER DEFAULT 600,
                status TEXT DEFAULT 'active'
            )
        ''')

        # Message library
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS message_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_link TEXT NOT NULL UNIQUE,
                collection_name TEXT,
                title TEXT,
                added_date DATETIME NOT NULL,
                times_used INTEGER DEFAULT 0,
                last_used DATETIME,
                success_rate REAL DEFAULT 0,
                is_archived INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')

        # Target tags
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS target_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                UNIQUE(group_id, tag)
            )
        ''')

        # Campaign schedules
        await self.conn.execute('''
            CREATE TABLE IF NOT EXISTS campaign_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id TEXT NOT NULL UNIQUE,
                schedule_type TEXT NOT NULL,
                schedule_time TEXT,
                schedule_days TEXT,
                last_run DATETIME,
                next_run DATETIME,
                is_active INTEGER DEFAULT 1
            )
        ''')

        # Create indices
        await self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_message_history_campaign ON message_history(campaign_id)'
        )
        await self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_message_history_group ON message_history(group_id)'
        )
        await self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_message_history_timestamp ON message_history(timestamp)'
        )
        await self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_campaign_runs_campaign ON campaign_runs(campaign_id)'
        )
        await self.conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_group_health_group ON group_health(group_id)'
        )

        await self.conn.commit()

    async def log_message_send(
        self,
        campaign_id: str,
        campaign_name: str,
        account_id: str,
        message_link: str,
        group_id: int,
        group_title: str,
        topic_id: Optional[int],
        topic_title: Optional[str],
        success: bool,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        send_duration_ms: Optional[int] = None,
        stars_cost: int = 0
    ):
        """Log a message send attempt."""
        if not self.conn:
            return

        await self.conn.execute('''
            INSERT INTO message_history (
                timestamp, campaign_id, campaign_name, account_id,
                message_link, group_id, group_title, topic_id, topic_title,
                success, error_type, error_message, send_duration_ms, stars_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(),
            campaign_id,
            campaign_name,
            account_id,
            message_link,
            group_id,
            group_title,
            topic_id,
            topic_title,
            1 if success else 0,
            error_type,
            error_message,
            send_duration_ms,
            stars_cost
        ))
        await self.conn.commit()

    async def start_campaign_run(
        self,
        campaign_id: str,
        campaign_name: str,
        account_id: str,
        total_scheduled: int
    ) -> int:
        """Log campaign run start."""
        if not self.conn:
            return -1

        cursor = await self.conn.execute('''
            INSERT INTO campaign_runs (
                campaign_id, campaign_name, account_id,
                start_time, total_scheduled, status
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            campaign_id,
            campaign_name,
            account_id,
            datetime.now().isoformat(),
            total_scheduled,
            'running'
        ))
        await self.conn.commit()
        return cursor.lastrowid

    async def update_campaign_run(
        self,
        run_id: int,
        total_sent: int,
        total_failed: int,
        status: str,
        stars_spent: int = 0
    ):
        """Update campaign run progress."""
        if not self.conn:
            return

        await self.conn.execute('''
            UPDATE campaign_runs
            SET total_sent = ?,
                total_failed = ?,
                status = ?,
                stars_spent = ?,
                end_time = ?
            WHERE id = ?
        ''', (
            total_sent,
            total_failed,
            status,
            stars_spent,
            datetime.now().isoformat() if status in ['completed', 'stopped', 'failed'] else None,
            run_id
        ))
        await self.conn.commit()

    async def update_group_health(
        self,
        group_id: int,
        group_title: str,
        is_member: bool,
        can_post: bool,
        is_paid: bool = False,
        stars_per_post: Optional[int] = None,
        member_count: Optional[int] = None
    ):
        """Update group health status."""
        if not self.conn:
            return

        # Calculate health score
        cursor = await self.conn.execute(
            'SELECT success_count, failure_count FROM group_health WHERE group_id = ?',
            (group_id,)
        )
        row = await cursor.fetchone()

        if row:
            success_count = row['success_count']
            failure_count = row['failure_count']
            total = success_count + failure_count
            health_score = int((success_count / total * 100)) if total > 0 else 100

            if not is_member:
                health_score = 0
            elif not can_post:
                health_score = min(health_score, 30)
        else:
            health_score = 100 if is_member and can_post else 0

        await self.conn.execute('''
            INSERT INTO group_health (
                group_id, group_title, last_check, is_member, can_post,
                is_paid, stars_per_post, member_count, health_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                group_title = excluded.group_title,
                last_check = excluded.last_check,
                is_member = excluded.is_member,
                can_post = excluded.can_post,
                is_paid = excluded.is_paid,
                stars_per_post = excluded.stars_per_post,
                member_count = excluded.member_count,
                health_score = excluded.health_score
        ''', (
            group_id,
            group_title,
            datetime.now().isoformat(),
            1 if is_member else 0,
            1 if can_post else 0,
            1 if is_paid else 0,
            stars_per_post,
            member_count,
            health_score
        ))
        await self.conn.commit()

    async def record_group_success(self, group_id: int):
        """Record successful send to group."""
        if not self.conn:
            return

        await self.conn.execute('''
            UPDATE group_health
            SET success_count = success_count + 1,
                last_success = ?
            WHERE group_id = ?
        ''', (datetime.now().isoformat(), group_id))
        await self.conn.commit()

    async def record_group_failure(self, group_id: int):
        """Record failed send to group."""
        if not self.conn:
            return

        await self.conn.execute('''
            UPDATE group_health
            SET failure_count = failure_count + 1,
                last_failure = ?
            WHERE group_id = ?
        ''', (datetime.now().isoformat(), group_id))
        await self.conn.commit()


# Global database instance
_db: Optional[Database] = None


async def get_database() -> Database:
    """Get or create global database instance."""
    global _db
    if _db is None:
        from app.utils.paths import DATABASE_FILE
        _db = Database(DATABASE_FILE)
        await _db.connect()
    return _db


async def close_database():
    """Close global database connection."""
    global _db
    if _db:
        await _db.close()
        _db = None
