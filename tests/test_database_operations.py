from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import accounts
from app.database.schema import Database


class AccountDatabaseTests(unittest.TestCase):
    def test_account_and_proxy_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_file = Path(directory) / "forwarder.db"
            with patch.object(accounts, "DATABASE_FILE", database_file):
                account_id = accounts.add_account(
                    label="Primary",
                    api_id=12345,
                    api_hash="example-api-hash",
                    phone="+10000000000",
                )
                self.assertTrue(accounts.set_active_account(account_id))
                self.assertEqual(accounts.get_active_account().id, account_id)

                self.assertTrue(
                    accounts.update_account_proxy(
                        account_id=account_id,
                        proxy_type="socks5",
                        proxy_host="127.0.0.1",
                        proxy_port=1080,
                        proxy_user="user",
                        proxy_pass="password",
                    )
                )
                proxy_id = accounts.add_account_proxy(
                    account_id=account_id,
                    label="Local proxy",
                    proxy_type="socks5",
                    proxy_host="127.0.0.1",
                    proxy_port=1080,
                    proxy_user=None,
                    proxy_pass=None,
                )
                self.assertEqual([proxy.id for proxy in accounts.list_account_proxies(account_id)], [proxy_id])

                self.assertTrue(
                    accounts.update_proxy_rotation_settings(
                        account_id=account_id,
                        mode="round_robin",
                        rotate_on_login=True,
                    )
                )
                self.assertTrue(
                    accounts.update_account_advanced(
                        account_id=account_id,
                        rate_multiplier=0.8,
                        send_window_start="09:00",
                        send_window_end="21:00",
                        send_days="all",
                    )
                )
                saved = accounts.get_account(account_id)
                self.assertEqual(saved.rate_multiplier, 0.8)
                self.assertEqual(saved.send_window_start, "09:00")

                self.assertTrue(accounts.delete_account_proxy(proxy_id))
                self.assertTrue(accounts.delete_account(account_id))
                self.assertEqual(accounts.list_accounts(), [])


class AnalyticsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_campaign_and_group_health_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "analytics.db")
            await database.connect()
            try:
                await database.log_message_send(
                    campaign_id="campaign-1",
                    campaign_name="Example",
                    account_id="account-1",
                    message_link="https://t.me/example/1",
                    group_id=-1001234567890,
                    group_title="Example group",
                    topic_id=None,
                    topic_title=None,
                    success=True,
                    send_duration_ms=50,
                )
                run_id = await database.start_campaign_run(
                    campaign_id="campaign-1",
                    campaign_name="Example",
                    account_id="account-1",
                    total_scheduled=2,
                )
                await database.update_campaign_run(
                    run_id=run_id,
                    total_sent=1,
                    total_failed=1,
                    status="completed",
                )
                await database.update_group_health(
                    group_id=-1001234567890,
                    group_title="Example group",
                    is_member=True,
                    can_post=True,
                )
                await database.record_group_success(-1001234567890)
                await database.record_group_failure(-1001234567890)

                history = await (await database.conn.execute("SELECT * FROM message_history")).fetchall()
                run = await (
                    await database.conn.execute("SELECT * FROM campaign_runs WHERE id = ?", (run_id,))
                ).fetchone()
                health = await (
                    await database.conn.execute(
                        "SELECT * FROM group_health WHERE group_id = ?",
                        (-1001234567890,),
                    )
                ).fetchone()

                self.assertEqual(len(history), 1)
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["total_sent"], 1)
                self.assertEqual(health["success_count"], 1)
                self.assertEqual(health["failure_count"], 1)
            finally:
                await database.close()


if __name__ == "__main__":
    unittest.main()
