from __future__ import annotations

import unittest

from run import _strip_emoji


class RunHelperTests(unittest.TestCase):
    def test_strip_emoji_preserves_plain_text(self) -> None:
        self.assertEqual(_strip_emoji("Telegram Forwarder 123"), "Telegram Forwarder 123")

    def test_strip_emoji_removes_supported_symbols_and_variation_selectors(self) -> None:
        self.assertEqual(_strip_emoji("Ready ✅ 🚀 🇧🇬"), "Ready   ")


if __name__ == "__main__":
    unittest.main()
