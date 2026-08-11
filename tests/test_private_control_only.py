from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import ApplicationHandlerStop

from app.alerts.telegram_bot import TelegramBotManager


class PrivateControlOnlyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = TelegramBotManager("token", "123456789")
        self.manager.bot = SimpleNamespace()

    async def test_group_message_is_silently_stopped_by_authorization_guard(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock(), text="ordinary group message")
        update = SimpleNamespace(
            callback_query=None,
            effective_chat=SimpleNamespace(type="supergroup"),
            effective_message=message,
            effective_user=SimpleNamespace(id=123456789),
            message=message,
        )

        with self.assertRaises(ApplicationHandlerStop):
            await self.manager._guard_authorized(update, SimpleNamespace())

        message.reply_text.assert_not_awaited()

    async def test_group_text_fallback_never_replies(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock(), text="ordinary group message")
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="group"),
            message=message,
        )

        await self.manager._on_text(update, SimpleNamespace())

        message.reply_text.assert_not_awaited()

    async def test_private_text_fallback_still_guides_the_controller(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock(), text="hello")
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private"),
            message=message,
        )
        self.manager.inline_panel.try_handle_text = AsyncMock(return_value=False)

        await self.manager._on_text(update, SimpleNamespace())

        message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
