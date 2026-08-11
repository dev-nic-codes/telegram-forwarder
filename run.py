import sys
import os
import io
import asyncio
import json
import urllib.parse
import urllib.request
from pathlib import Path

# Add source directory to path (works for source and packaged EXE)
base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
sys.path.insert(0, str(base_dir / "source"))


def _suppress_windows_shutdown_noise() -> None:
    # Suppress noisy Proactor shutdown error on Windows (Python 3.13/3.14)
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        if hasattr(loop, "set_exception_handler"):
            def _handler(loop, context):
                exc = context.get("exception")
                if exc and isinstance(exc, AttributeError):
                    msg = str(exc)
                    if "shutdown" in msg and "NoneType" in msg:
                        return
                loop.default_exception_handler(context)
            loop.set_exception_handler(_handler)
    except Exception:
        pass


def _configure_utf8_console() -> None:
    # Avoid UnicodeEncodeError on Windows console (emojis, symbols)
    if os.name == "nt":
        try:
            import ctypes  # noqa: WPS433
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


_EMOJI_RANGES = (
    (0x1F1E6, 0x1F1FF),  # flags
    (0x1F300, 0x1F5FF),  # symbols and pictographs
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F680, 0x1F6FF),  # transport and map symbols
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
    (0x2600, 0x26FF),    # miscellaneous symbols
    (0x2700, 0x27BF),    # dingbats
)
_EMOJI_CODEPOINTS = frozenset({0xFE0F})  # variation selector


def _strip_emoji(text: str) -> str:
    def is_emoji(character: str) -> bool:
        codepoint = ord(character)
        return codepoint in _EMOJI_CODEPOINTS or any(
            start <= codepoint <= end for start, end in _EMOJI_RANGES
        )

    return "".join(character for character in text if not is_emoji(character))


class _EmojiStrippingStream(io.TextIOBase):
    def __init__(self, stream):
        self._stream = stream

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        return self._stream.write(_strip_emoji(s))

    def flush(self):
        return self._stream.flush()

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def fileno(self):
        return self._stream.fileno()

    def writable(self):
        return True


def _strip_emojis_when_frozen() -> None:
    if getattr(sys, "frozen", False):
        sys.stdout = _EmojiStrippingStream(sys.stdout)
        sys.stderr = _EmojiStrippingStream(sys.stderr)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _send_offline_alert(reason: str) -> None:
    # Best-effort crash/startup alert without importing app modules.
    try:
        appdata = os.environ.get("APPDATA", str(Path.home()))
        cfg_path = Path(appdata) / "TelegramForwarder" / "data" / "bot_config.json"
        if not cfg_path.exists():
            return
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not cfg or not cfg.get("enabled", False):
            return
        token = cfg.get("token")
        chat_id = cfg.get("chat_id")
        if not token or not chat_id:
            return
        text = (
            "🔌 Telegram Forwarder is going offline.\n"
            "🧯 Ads and bot are stopping now.\n"
            f"⚠️ Reason: {reason}"
        )
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # The scheme and host are fixed; only the bot token is placed in the
        # path for this best-effort shutdown notification.
        urllib.request.urlopen(url, data=data, timeout=5)  # nosec B310
    except Exception:
        pass


if __name__ == "__main__":
    _configure_utf8_console()
    _strip_emojis_when_frozen()
    _suppress_windows_shutdown_noise()
    try:
        from app.main import main
    except Exception as e:
        _send_offline_alert(f"Startup error: {type(e).__name__}: {e}")
        raise
    main()
