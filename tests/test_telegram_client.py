from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.telegram_client import TgClient


class TelegramClientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_session_retry_does_not_block_event_loop(self) -> None:
        client = object.__new__(TgClient)
        client._session_path = Path("test-session")
        client._client = SimpleNamespace(
            connect=AsyncMock(
                side_effect=[sqlite3.OperationalError("database is locked"), None]
            ),
            is_user_authorized=AsyncMock(return_value=True),
        )

        with patch("app.core.telegram_client.asyncio.sleep", new=AsyncMock()) as sleep:
            await client.connect_and_login()

        sleep.assert_awaited_once_with(0.4)
        self.assertEqual(client._client.connect.await_count, 2)

    async def test_locked_disconnect_retry_does_not_block_event_loop(self) -> None:
        client = object.__new__(TgClient)
        client._client = SimpleNamespace(
            disconnect=AsyncMock(
                side_effect=[sqlite3.OperationalError("database is locked"), None]
            )
        )

        with patch("app.core.telegram_client.asyncio.sleep", new=AsyncMock()) as sleep:
            await client.close()

        sleep.assert_awaited_once_with(0.4)
        self.assertEqual(client._client.disconnect.await_count, 2)


if __name__ == "__main__":
    unittest.main()
