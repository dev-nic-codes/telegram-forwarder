# app/alerts/telegram_bot.py


"""Telegram bot for alerts and remote control"""


from __future__ import annotations





import asyncio

import warnings

from typing import Optional, Callable, Awaitable, Any


from datetime import datetime


import threading




from dataclasses import dataclass







from telegram import Update, Bot


from telegram import BotCommand, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from telegram.error import InvalidToken, Conflict, TimedOut


import logging


from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)





from app.utils.paths import DATA_DIR, PROFILES_DIR


from app.utils.storage import load_json, save_json


from app.utils.state import load_state, pause_state, resume_state, stop_state


from app.core.campaigns import list_campaigns, get_campaign, replace_campaign


from app.analytics.history_tracker import get_history
from app.alerts.control_panel import ForwarderInlineControlPanel





def _estimate_send_rates(

    *,


    send_gap_min: int,


    send_gap_max: int,


    batch_gap_min: int,


    batch_gap_max: int,


    batch_size: int,


) -> tuple[float, float]:


    sg = (send_gap_min + send_gap_max) / 2.0


    bg = (batch_gap_min + batch_gap_max) / 2.0


    bs = max(1, int(batch_size))


    avg_gap = sg + (bg / bs)


    if avg_gap <= 0:


        return 0.0, 0.0


    per_hour = 3600.0 / avg_gap


    per_day = per_hour * 24.0


    return per_hour, per_day





def _friendly_error(name: str) -> str:

    key = (name or "").strip()

    mapping = {

        "SlowModeWaitError": "Slow mode (wait required)",

        "ConnectionError": "Connection issue",

        "OperationalError": "Database locked",

        "ValueError": "Invalid data",

        "ForbiddenError": "No permission",
        "ChatWriteForbiddenError": "Cannot send to this destination",
        "ChannelPrivateError": "Destination is no longer accessible",
        "ChatSendMediaForbiddenError": "Media is not allowed in this destination",
        "ChatSendPhotosForbiddenError": "Photos are not allowed in this destination",
        "ChatSendVideosForbiddenError": "Videos are not allowed in this destination",
        "MessageIdInvalidError": "Source message is unavailable",
        "ChatForwardsRestrictedError": "Source content is protected from forwarding",

    }

    return mapping.get(key, key or "Error")





def _format_ts(value: object) -> str:

    if isinstance(value, datetime):

        return value.strftime("%H:%M:%S %d/%m/%Y")

    if isinstance(value, str) and value:

        try:

            return datetime.fromisoformat(value).strftime("%H:%M:%S %d/%m/%Y")

        except Exception:

            pass

    return str(value)[:19] if value is not None else "-"





@dataclass

class BotControl:

    list_running: Callable[[], Awaitable[str]]


    stop_running: Callable[[str], Awaitable[str]]


    start_ad: Callable[[str, bool, bool], Awaitable[str]]

    resume_schedule: Callable[[str], Awaitable[str]]

    ad_status: Callable[[str], Awaitable[str]]


    enable_ad: Callable[[str], Awaitable[str]]


    disable_ad: Callable[[str], Awaitable[str]]


    health: Callable[[], Awaitable[str]]


    list_disabled: Callable[[str], Awaitable[str]]


    clear_disabled: Callable[[str], Awaitable[str]]
    dashboard_status: Callable[[], Awaitable[dict]] | None = None
    recent_logs: Callable[[int], Awaitable[str]] | None = None
    reload_settings: Callable[[], None] | None = None
    scan_groups: Callable[[], Awaitable[str]] | None = None
    set_group_enabled: Callable[[int, bool], Awaitable[str]] | None = None
    set_group_topic_enabled: Callable[[int, int, bool], Awaitable[str]] | None = None
    add_source: Callable[[str], Awaitable[str]] | None = None
    remove_source: Callable[[int], Awaitable[str]] | None = None
    reload_ad: Callable[[str], Awaitable[str]] | None = None








class TelegramBotManager:


    """Manage Telegram bot for alerts and commands"""





    def __init__(self, token: str, chat_id: str, control: BotControl | None = None):


        self.token = token


        self.chat_id = chat_id
        self.allowed_user_id = int(chat_id)


        self.bot: Optional[Bot] = None


        self.app: Optional[Application] = None


        self.last_error: Optional[str] = None


        self.control: Optional[BotControl] = control
        self.inline_panel = ForwarderInlineControlPanel(self)
        self._menu_cleanup_done: set[int] = set()


        self._action_ui: dict[int, dict[str, Any]] = {}


        self._thread: Optional[threading.Thread] = None


        self._loop: Optional[asyncio.AbstractEventLoop] = None


        self._start_event = threading.Event()


        self._start_error: Optional[Exception] = None





    async def start(self):

        """Start the bot"""

        warnings.filterwarnings(

            "ignore",

            message=r".*Application` instances should be built via the `ApplicationBuilder`.*",

            category=UserWarning,

        )

        for name in ("telegram", "telegram.ext", "telegram.ext._updater"):

            logger = logging.getLogger(name)


            logger.setLevel(logging.CRITICAL)


            logger.propagate = False


        self.app = Application.builder().token(self.token).build()

        # Reject every command, text input, and callback from non-controller users.
        self.app.add_handler(TypeHandler(Update, self._guard_authorized), group=-1)





        # Register command handlers


        self.app.add_handler(CommandHandler("start", self.cmd_start))


        self.app.add_handler(CommandHandler("help", self.cmd_help))


        self.app.add_handler(CommandHandler("menu", self.cmd_menu))


        self.app.add_handler(CommandHandler("status", self.cmd_status))


        self.app.add_handler(CommandHandler("ads", self.cmd_ads))


        self.app.add_handler(CommandHandler("next", self.cmd_next))

        self.app.add_handler(CommandHandler("stats", self.cmd_stats))

        self.app.add_handler(CommandHandler("today", self.cmd_today))

        self.app.add_handler(CommandHandler("running", self.cmd_running))

        self.app.add_handler(CommandHandler("health", self.cmd_health))

        self.app.add_handler(CommandHandler("recent", self.cmd_recent))

        self.app.add_handler(CommandHandler("errors", self.cmd_errors))
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop))
        self.app.add_handler(CommandHandler("stoprun", self.cmd_stoprun))
        self.app.add_handler(CommandHandler("startad", self.cmd_startad))
        self.app.add_handler(CommandHandler("ad", self.cmd_ad))
        self.app.add_handler(CommandHandler("enable", self.cmd_enable))
        self.app.add_handler(CommandHandler("disable", self.cmd_disable))
        self.app.add_handler(CommandHandler("showid", self.cmd_showid))
        self.app.add_handler(CommandHandler("disabled", self.cmd_disabled))
        self.app.add_handler(CommandHandler("cleardisabled", self.cmd_cleardisabled))
        self.app.add_handler(CommandHandler("admenu", self.cmd_admenu))
        self.app.add_handler(CallbackQueryHandler(self.inline_panel.on_callback, pattern=r"^fw:"))

        self.app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                self._on_text,
            )
        )

        self.app.add_error_handler(self._on_error)





        # Start polling in background


        try:


            await self.app.initialize()


            self.bot = self.app.bot


            try:


                await self.bot.get_me()


            except InvalidToken as e:


                raise RuntimeError("Invalid bot token") from e


            try:


                await self._set_commands()


            except Exception:


                pass


            await self.app.start()


            try:


                if not self.bot:


                    self.bot = self.app.bot


                try:


                    await self.bot.delete_webhook(drop_pending_updates=True)


                except Exception:


                    pass


                await self.app.updater.start_polling(drop_pending_updates=True, poll_interval=0.5)


            except Conflict:


                self.last_error = "Bot polling conflict: another instance is running."


                await self.stop()


                return False


            await self._send_menu()

            cfg = load_bot_config() or {}

            alert_mode = str(cfg.get("alert_mode", DEFAULT_ALERT_MODE))

            alert_every_n = int(cfg.get("alert_every_n", DEFAULT_ALERT_EVERY_N) or DEFAULT_ALERT_EVERY_N)

            if alert_mode == "every":

                alert_txt = "All events"

            elif alert_mode == "summary":

                alert_txt = f"Summary (every {alert_every_n} sends)"

            else:

                alert_txt = "Errors only"

            await self.send_message(

                "Bot is online and active.\n"

                f"Alert setting: {alert_txt}"

            )

            return True

        except Exception as e:


            await self.stop()


            raise RuntimeError(f"Failed to start bot: {e}") from e

    async def _guard_authorized(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat = update.effective_chat
        if chat is not None and str(chat.type).lower() != "private":
            # The bot is a private control surface. Group messages must never
            # produce control prompts or authorization replies.
            if update.callback_query is not None:
                await update.callback_query.answer()
            raise ApplicationHandlerStop
        user = update.effective_user
        if user is not None and int(user.id) == self.allowed_user_id:
            return
        if update.callback_query is not None:
            await update.callback_query.answer("Not authorized.", show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text("Not authorized.")
        raise ApplicationHandlerStop





    async def start_with_takeover(self) -> None:


        """Start bot and automatically attempt takeover once on conflict."""


        ok = await self.start()


        if ok:


            return True


        await self.force_takeover()


        ok = await self.start()


        return ok





    async def force_takeover(self) -> None:


        """Attempt to take over updates by clearing webhook and stopping any local app."""


        if not self.bot:


            self.bot = Bot(token=self.token)


        try:


            await self.bot.delete_webhook(drop_pending_updates=True)


        except Exception:


            pass


        try:


            if self.app and self.app.updater:


                await self.app.updater.stop()


        except Exception:


            pass


        try:


            if self.app:


                await self.app.stop()


        except Exception:


            pass


        try:


            if self.app:


                await self.app.shutdown()


        except Exception:


            pass


        self.app = None





    def start_background(self, timeout_sec: int = 10) -> None:


        """Start bot in a dedicated thread/event loop for responsive commands."""


        if self._thread and self._thread.is_alive():


            return


        self._start_event.clear()


        self._start_error = None





        def _runner() -> None:


            self._loop = asyncio.new_event_loop()


            asyncio.set_event_loop(self._loop)





            async def _start_task() -> None:


                try:


                    ok = await self.start_with_takeover()


                    if not ok:


                        self._start_error = RuntimeError(


                            "Bot polling conflict: another instance is running."


                        )


                except Exception as e:


                    self._start_error = e


                finally:


                    self._start_event.set()


                    if self._start_error:


                        self._loop.stop()





            self._loop.create_task(_start_task())


            self._loop.run_forever()





        self._thread = threading.Thread(target=_runner, daemon=True)


        self._thread.start()


        self._start_event.wait(timeout=timeout_sec)


        if self._start_error:


            raise RuntimeError(str(self._start_error)) from self._start_error





    def stop_background(self, timeout_sec: int = 10) -> None:


        """Stop bot running in background thread."""


        if not self._loop:


            return


        try:


            fut = asyncio.run_coroutine_threadsafe(self.stop(), self._loop)


            fut.result(timeout=timeout_sec)


        except Exception:


            pass


        try:


            if self._loop.is_running():


                self._loop.call_soon_threadsafe(self._loop.stop)


        except Exception:


            pass


        try:


            if self._thread and self._thread.is_alive():


                self._thread.join(timeout=timeout_sec)


        except Exception:


            pass


        self._loop = None


        self._thread = None





    def set_control(self, control: BotControl | None) -> None:


        self.control = control





    async def stop(self):


        """Stop the bot"""


        if self.app:


            try:


                if self.app.updater:


                    await self.app.updater.stop()


            except Exception:


                pass


            try:


                await self.app.stop()


            except Exception:


                pass


            try:


                await self.app.shutdown()


            except Exception:


                pass





    async def _on_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:


        err = context.error


        if isinstance(err, Conflict):


            self.last_error = "Conflict: another getUpdates request is running for this bot token."


            try:


                print(


                    "Bot error: Conflict detected. Another bot instance is already polling for updates."


                )


            except Exception:


                pass


            try:


                await self.stop()


            except Exception:


                pass





    async def send_message(self, text: str):


        """Send a message to the configured chat"""


        if not self.bot:


            return


        try:


            await self.bot.send_message(chat_id=self.chat_id, text=text)


        except (TimedOut, Exception):


            return





    def send_message_background(self, text: str, timeout_sec: int = 5) -> None:


        if self._loop and self._loop.is_running():


            try:


                fut = asyncio.run_coroutine_threadsafe(self.send_message(text), self._loop)


                fut.result(timeout=timeout_sec)


            except Exception:


                pass


            return


        try:


            asyncio.run(self.send_message(text))


        except Exception:


            pass





    async def _set_commands(self) -> None:


        if not self.bot:


            return


        commands = [
            BotCommand("menu", "Open the control center"),
            BotCommand("admenu", "Open the ad action menu"),
            BotCommand("status", "Show current ad status"),
            BotCommand("ads", "List all ads"),
            BotCommand("running", "List running ads"),
            BotCommand("next", "Show next scheduled send"),
            BotCommand("stats", "Show statistics"),
            BotCommand("today", "Show today's summary"),
            BotCommand("health", "Show bot/app health"),
            BotCommand("recent", "Show recent sends"),
            BotCommand("errors", "Show recent errors"),
            BotCommand("ad", "Show one ad status"),
            BotCommand("startad", "Start one ad"),
            BotCommand("pause", "Pause one ad"),
            BotCommand("resume", "Resume one ad"),
            BotCommand("stop", "Stop one ad"),
            BotCommand("enable", "Enable one ad"),
            BotCommand("disable", "Disable one ad"),
            BotCommand("disabled", "Show disabled targets"),
            BotCommand("cleardisabled", "Clear disabled targets"),
            BotCommand("showid", "List ad IDs"),
            BotCommand("help", "Show help"),
        ]

        await self.bot.set_my_commands(commands)



    def _menu_keyboard(self) -> ReplyKeyboardMarkup:

        rows = [
            [KeyboardButton("🎛️ Ad Menu"), KeyboardButton("📊 Status")],
            [KeyboardButton("📣 Ads"), KeyboardButton("🏃 Running")],
            [KeyboardButton("⏱️ Next"), KeyboardButton("📈 Stats")],
            [KeyboardButton("📅 Today"), KeyboardButton("🩺 Health")],
            [KeyboardButton("📝 Recent"), KeyboardButton("❌ Errors")],
            [KeyboardButton("🆔 IDs"), KeyboardButton("📚 Help")],
        ]

        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)




    async def _send_menu(self, chat_id: Optional[int] = None) -> None:
        if not self.bot:
            return
        target = int(chat_id or self.chat_id)
        if target not in self._menu_cleanup_done:
            await self.bot.send_message(
                chat_id=target,
                text="🧹 Switched to the inline control center.",
                reply_markup=ReplyKeyboardRemove(),
            )
            self._menu_cleanup_done.add(target)
        await self.inline_panel.send_home(target)



    async def _reply(
        self,
        update: Update,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        show_menu: bool = False,
    ) -> None:
        if not update.message:
            return
        kwargs: dict[str, Any] = {}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        await update.message.reply_text(text, **kwargs)

    def _dashboard_text(self) -> str:
        return (
            "🤖 Telegram Forwarder Control Center\n\n"
            "Use the buttons below to inspect ads, open the ad action panel, and check health, history, statistics, and errors.\n\n"
            "Tap 🎛️ Ad Menu to control any saved ad."
        )

    def _menu_text(self) -> str:
        return (
            "🤖 Telegram Forwarder Bot Menu\n\n"
            "⚡ Quick Actions\n"
            "⚙️ /menu - Ad controls with buttons\n\n"
            "📊 Monitoring\n"
            "📈 /status - Ad status overview\n"
            "📣 /ads - List all ads\n"
            "🏃 /running - Running ads\n"
            "⏱️ /next - Next scheduled send\n"
            "💊 /health - Bot/app health\n"
            "📝 /recent [n] - Recent sends\n"
            "❌ /errors [n] - Recent errors\n"
            "📈 /stats - Show statistics\n"
            "📅️ /today - Today's summary\n\n"
            "Other\n"
            "❓ /help - Show help"
        )

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        if self.control:
            await self._reply(
                update,
                "🤖 <b>Telegram Forwarder Bot</b>\n\n"
                "This bot controls the live forwarder service, edits saved ads, and shows runtime health, recent activity, and live logs.",
                parse_mode="HTML",
            )
            await self._send_menu(chat_id=update.effective_chat.id)
            return
        await self._reply(
            update,
            "🤖 Telegram Forwarder Bot\n\n"
            "This bot controls the live forwarder service, starts and stops saved ads, and shows runtime health and history.\n\n"
            "Use the dashboard buttons below or open 🎛️ Ad Menu for per-ad actions.",
            show_menu=True,
        )
        await self._send_menu(chat_id=update.effective_chat.id)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        if self.control:
            await self._reply(
                update,
                "❓ <b>Telegram Forwarder Help</b>\n\n"
                "Use /menu to open the inline control center.\n"
                "You can inspect ads, edit pacing and schedule fields, view recent activity, and read live runtime logs.\n\n"
                "Direct commands still work too: /status, /ads, /running, /recent, /errors, /health.",
                parse_mode="HTML",
            )
            return
        await self._reply(
            update,
            "📚 Telegram Forwarder Help Center\n"
            "----------------\n\n"
            "🎛️ Dashboards\n"
            "• /menu - Main control center\n"
            "• /admenu - Ad action menu\n"
            "• /status, /ads, /running, /next, /stats, /today, /health\n"
            "• /recent [n], /errors [n], /showid\n\n"
            "🛠️ Direct ad commands\n"
            "• /ad <ad_id>\n"
            "• /startad <ad_id> <dry|live> [force]\n"
            "• /pause <ad_id>, /resume <ad_id>, /stop <ad_id>\n"
            "• /enable <ad_id>, /disable <ad_id>\n"
            "• /disabled <ad_id>, /cleardisabled <ad_id>\n\n"
            "✨ Tip\n"
            "The emoji keyboard is the fastest way to run the whole service from Telegram.",
            show_menu=True,
        )

    def _require_control(self) -> Optional[str]:
        if self.control is None:
            return "Control interface not available in this instance."
        return None

    def _parse_limit(self, arg: Optional[str], *, default: int = 10, cap: int = 50) -> int:
        if not arg:
            return default
        try:
            n = int(arg)
        except Exception:
            return default
        return max(1, min(cap, n))

    def _action_ui_state(self, chat_id: int) -> dict[str, Any]:


        state = self._action_ui.get(chat_id)


        if not state:


            state = {"action": "start_dry"}


            self._action_ui[chat_id] = state


        return state





    def _ad_menu_keyboard(self, *, chat_id: int) -> ReplyKeyboardMarkup:


        ads = list_campaigns()


        state = self._action_ui_state(chat_id)


        action = state.get("action", "start_dry")





        rows = [
            [KeyboardButton("🧪 Start DRY"), KeyboardButton("🚀 Start LIVE")],
            [KeyboardButton("🔁 Resume Schedule")],
            [KeyboardButton("🧪 Force DRY"), KeyboardButton("🚀 Force LIVE")],
            [KeyboardButton("⏸️ Pause"), KeyboardButton("▶️ Resume"), KeyboardButton("⏹️ Stop")],
            [KeyboardButton("✅ Enable"), KeyboardButton("⛔ Disable")],
            [KeyboardButton("ℹ️ Status"), KeyboardButton("🚫 Disabled Targets")],
            [KeyboardButton("🧹 Clear Disabled"), KeyboardButton("🔄 Refresh Ads")],
            [KeyboardButton("🏠 Main Menu"), KeyboardButton("❌ Close")],
        ]





        action_labels = {
            "start_dry": "🧪 START DRY",
            "start_live": "🚀 START LIVE",
            "resume_schedule": "🔁 RESUME SCHEDULE",
            "start_dry_force": "🧪 FORCE DRY",
            "start_live_force": "🚀 FORCE LIVE",
            "pause": "⏸️ PAUSE",
            "resume": "▶️ RESUME",
            "stop": "⏹️ STOP",
            "enable": "✅ ENABLE",
            "disable": "⛔ DISABLE",
            "status": "ℹ️ STATUS",
            "disabled": "🚫 DISABLED TARGETS",
            "clear_disabled": "🧹 CLEAR DISABLED",
        }
        action_label = action_labels.get(action, action.replace("_", " ").upper())


        rows.insert(0, [KeyboardButton(f"🎯 Action: {action_label}")])





        for c in ads[:20]:


            rows.append([KeyboardButton(f"📌 {c.name}")])





        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)





    async def _send_ad_menu(self, chat_id: int) -> None:


        state = self._action_ui_state(chat_id)


        action = state.get("action", "start_dry")


        await self.bot.send_message(


            chat_id=chat_id,


            text=(
                "🎛️ Ad Control Menu\n\n"
                f"🎯 Current action: {action.replace('_', ' ').upper()}\n"
                "Choose an action, then tap an ad below.\n"
                "This panel controls the live service state."
            ),

            reply_markup=self._ad_menu_keyboard(chat_id=chat_id),


        )





    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /menu command"""
        if not self.bot:
            return
        await self._send_menu(chat_id=update.effective_chat.id)





    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /status command"""


        ads = list_campaigns()


        active = 0


        paused = 0


        idle = 0


        disabled = 0





        for c in ads:


            if not getattr(c, "enabled", True):


                disabled += 1


                continue


            state_path = PROFILES_DIR / f"state_{c.id}.json"


            st = load_state(state_path, c.id)


            if st is None:


                idle += 1


            elif getattr(st, "paused", False):


                paused += 1


            else:


                active += 1





        await self._reply(
            update,
            f"\U0001F4CA Ad Status\n\n"
            f"\u2705 Active: {active}\n"
            f"\u23f8\ufe0f Paused: {paused}\n"
            f"\u23f3 Idle: {idle}\n"
            f"\u26d4 Disabled: {disabled}",
        )




    async def cmd_ads(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /ads command"""


        ads = list_campaigns()


        if not ads:


            await self._reply(update, "\U0001F4E3 Ads\n\nNo ads found yet.")

            return

        lines = ["\U0001F4E3 Ads"]

        for i, c in enumerate(ads, start=1):


            if not getattr(c, "enabled", True):


                status = "\u26d4 Disabled"


            else:


                state_path = PROFILES_DIR / f"state_{c.id}.json"


                st = load_state(state_path, c.id)


                if st is None:


                    status = "\u23f3 Idle"


                elif getattr(st, "paused", False):


                    status = "\u23f8\ufe0f Paused"


                else:


                    status = "\u2705 Active"


            lines.append(f"{i}. {c.name}  {status}")




        await self._reply(update, "\n".join(lines))




    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /pause command"""


        if not context.args:


            await update.message.reply_text("Usage: /pause <ad_id>")


            return





        ad_id = context.args[0]


        c = get_campaign(ad_id)


        if not c:


            await update.message.reply_text(f"Ad not found: {ad_id}")


            return


        state_path = PROFILES_DIR / f"state_{ad_id}.json"


        ok = pause_state(state_path, ad_id)


        await update.message.reply_text(


            f"Paused ad: {ad_id}" if ok else "No state found for this ad yet."


        )





    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /resume command"""


        if not context.args:


            await update.message.reply_text("Usage: /resume <ad_id>")


            return





        ad_id = context.args[0]


        c = get_campaign(ad_id)


        if not c:


            await update.message.reply_text(f"Ad not found: {ad_id}")


            return


        state_path = PROFILES_DIR / f"state_{ad_id}.json"


        ok = resume_state(state_path, ad_id)


        if ok and not getattr(c, "enabled", True):


            c.enabled = True


            replace_campaign(c)


        await update.message.reply_text(


            f"Resumed ad: {ad_id} (enabled)" if ok else "No state found for this ad yet."


        )





    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /stop command"""


        if not context.args:


            await update.message.reply_text("Usage: /stop <ad_id>")


            return





        ad_id = context.args[0]


        c = get_campaign(ad_id)


        if not c:


            await update.message.reply_text(f"Ad not found: {ad_id}")


            return


        state_path = PROFILES_DIR / f"state_{ad_id}.json"


        stopped = stop_state(state_path, ad_id)


        await update.message.reply_text(


            f"Stopped ad: {ad_id} (still enabled)" if stopped else "No state found for this ad yet."


        )





    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /stats command"""


        try:


            history = await get_history()


            totals = await history.get_totals()


            total = int(totals.get("total") or 0)


            successful = int(totals.get("successful") or 0)


            success_rate = (successful / total * 100) if total > 0 else 0.0


            active = 0


            cap_lines: list[str] = []


            for c in list_campaigns():


                if not getattr(c, "enabled", True):


                    continue


                state_path = PROFILES_DIR / f"state_{c.id}.json"


                st = load_state(state_path, c.id)


                if st is not None and not getattr(st, "paused", False):


                    active += 1


                daily_cap = getattr(c, "daily_cap", None)


                if isinstance(daily_cap, int) and daily_cap > 0:


                    sent_today = int(getattr(st, "day_sent_count", 0) or 0) if st else 0


                    remaining = max(0, int(daily_cap) - sent_today)


                    per_hour, _ = _estimate_send_rates(


                        send_gap_min=c.send_gap_min_sec,


                        send_gap_max=c.send_gap_max_sec,


                        batch_gap_min=c.batch_gap_min_sec,


                        batch_gap_max=c.batch_gap_max_sec,


                        batch_size=max(1, len(c.target_refs)),


                    )


                    eta_txt = ""


                    if per_hour > 0 and remaining > 0:


                        eta_minutes = int(round((remaining / per_hour) * 60))


                        eta_h = eta_minutes // 60


                        eta_m = eta_minutes % 60


                        eta_txt = f", ETA ~{eta_h}h {eta_m}m" if eta_h > 0 else f", ETA ~{eta_m}m"


                    cap_lines.append(f"- {c.name} (id={c.id}): {sent_today}/{daily_cap} ({remaining} left){eta_txt}")





            caps_txt = ""


            if cap_lines:


                if len(cap_lines) > 10:


                    cap_lines = cap_lines[:10] + ["(more ads with caps)"]


                caps_txt = "\n\n\U0001F4C5 Daily cap progress:\n" + "\n".join(cap_lines)





            await self._reply(
                update,
                f"\U0001F4C8 Statistics\n\n"
                f"\U0001F4E8 Total messages: {total}\n"
                f"\u2705 Success rate: {success_rate:.1f}%\n"
                f"\U0001F4CC Active ads: {active}"
                f"{caps_txt}",
            )

        except Exception as e:

            await self._reply(update, f"Error loading stats: {e}")




    async def cmd_next(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /next command"""


        ads = list_campaigns()


        if not ads:


            await self._reply(update, "\u23f1\ufe0f Next Send\n\nNo ads found yet.")

            return




        upcoming: list[tuple[str, datetime, str]] = []


        now = datetime.now()


        for c in ads:


            if not getattr(c, "enabled", True):


                continue


            state_path = PROFILES_DIR / f"state_{c.id}.json"


            st = load_state(state_path, c.id)


            if st is None:


                continue


            na = getattr(st, "next_at", None)


            if isinstance(na, str):


                try:


                    na = datetime.fromisoformat(na)


                except Exception:


                    na = None


            if isinstance(na, datetime):


                if na < now:


                    overdue_sec = int((now - na).total_seconds())


                    note = f"(due now, overdue {overdue_sec}s)"


                else:


                    note = ""


                upcoming.append((c.name, na, note))





        if not upcoming:


            await self._reply(update, "\u23f1\ufe0f Next Send\n\nNo upcoming sends are scheduled.")

            return




        upcoming.sort(key=lambda x: x[1])


        lines = ["\u23f1\ufe0f Next Send"]

        for name, dt, note in upcoming[:5]:

            ts = dt.strftime("%H:%M:%S %d/%m/%Y")

            lines.append(f"- {name}: {ts} {note}".rstrip())

        await self._reply(update, "\n".join(lines))




    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /today command"""


        try:


            history = await get_history()


            stats = await history.get_stats(days=1)


            total = int(stats.get("total") or 0)


            successful = int(stats.get("successful") or 0)


            failed = int(stats.get("failed") or 0)


            success_rate = (successful / total * 100) if total > 0 else 0.0





            await self._reply(

                update,

                f"\U0001F4C5 Today's Summary ({datetime.now().strftime('%H:%M:%S %d/%m/%Y')})\n\n"

                f"\U0001F4E8 Messages: {total}\n"

                f"\u2705 Successful: {successful} ({success_rate:.1f}%)\n"

                f"\u274c Failed: {failed}",

            )

        except Exception as e:

            await self._reply(update, f"Error loading today's stats: {e}")




    async def cmd_running(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /running command"""


        err = self._require_control()

        if err:

            await self._reply(update, err)

            return

        msg = await self.control.list_running()

        await self._reply(update, msg)




    async def cmd_stoprun(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /stoprun command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /stoprun <ad_id>")


            return


        ad_id = context.args[0]


        msg = await self.control.stop_running(ad_id)


        await update.message.reply_text(msg)





    async def cmd_startad(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /startad command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args or len(context.args) < 2:


            await update.message.reply_text("Usage: /startad <ad_id> <dry|live> [force]")


            return


        ad_id = context.args[0]


        mode = context.args[1].strip().lower()


        if mode not in ("dry", "live"):


            await update.message.reply_text("Mode must be 'dry' or 'live'.")


            return


        force = len(context.args) >= 3 and context.args[2].strip().lower() == "force"


        msg = await self.control.start_ad(ad_id, mode == "dry", force)


        await update.message.reply_text(msg)





    async def cmd_ad(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /ad command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /ad <ad_id>")


            return


        try:


            ad_id = context.args[0]


            msg = await self.control.ad_status(ad_id)


            await update.message.reply_text(msg)


        except Exception as e:


            await update.message.reply_text(f"Error loading ad status: {e}")





    async def cmd_enable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /enable command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /enable <ad_id>")


            return


        ad_id = context.args[0]


        msg = await self.control.enable_ad(ad_id)


        await update.message.reply_text(msg)





    async def cmd_disable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /disable command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /disable <ad_id>")


            return


        ad_id = context.args[0]


        msg = await self.control.disable_ad(ad_id)


        await update.message.reply_text(msg)





    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /health command"""


        err = self._require_control()

        if err:

            await self._reply(update, err)

            return

        msg = await self.control.health()

        await self._reply(update, msg)




    async def cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /recent command"""


        limit = self._parse_limit(context.args[0] if context.args else None, default=10, cap=50)


        try:


            history = await get_history()


            records = await history.get_recent(limit=limit)

            if not records:

                await self._reply(update, "\U0001F4DD Recent sends\n\nNo history found yet.")

                return

            lines = ["\U0001F4DD Recent sends"]

            for r in records[:limit]:

                ts = _format_ts(r.get("timestamp", ""))

                ad = r.get("campaign_name", "Unknown")

                group = r.get("group_title", "Unknown")

                status = "\u2705 OK" if r.get("success") else "\u274c FAIL"
                lines.append(f"- {ts} | {status} | {ad} -> {group}")

            await self._reply(update, "\n".join(lines))

        except Exception as e:

            await self._reply(update, f"Error loading recent history: {e}")




    async def cmd_errors(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /errors command"""


        limit = self._parse_limit(context.args[0] if context.args else None, default=10, cap=50)


        try:


            history = await get_history()


            records = await history.get_recent_errors(limit=limit)

            if not records:

                await self._reply(update, "\u274c Recent errors\n\nNo errors found.")

                return

            lines = ["\u274c Recent errors"]

            for r in records[:limit]:

                ts = _format_ts(r.get("timestamp", ""))

                ad = r.get("campaign_name", "Unknown")

                group = r.get("group_title", "Unknown")

                raw_err = r.get("error_type") or "Error"

                err_type = self._pretty_error(raw_err)

                lines.append(f"- {ts} | {ad} -> {group} | \u26a0\ufe0f {err_type}")

            await self._reply(update, "\n".join(lines))

        except Exception as e:

            await self._reply(update, f"Error loading errors: {e}")




    def _pretty_error(self, err_type: str) -> str:

        raw = (err_type or "").strip()

        if not raw:

            return "Error"

        mapped = _friendly_error(raw)

        if mapped != raw:

            return mapped

        lower = raw.lower()

        if "slowmode" in lower:

            return "Slow Mode"

        if "flood" in lower:

            return "Flood Wait"

        if "timeout" in lower:

            return "Timeout"

        if "forbidden" in lower:

            return "No Permission"

        if "chatadminrequired" in lower:

            return "Admin Required"

        if "topicclosed" in lower:

            return "Topic Closed"

        if "userbannedinchannel" in lower:

            return "Banned in Channel"

        if "messageidinvalid" in lower:

            return "Message Not Found"

        if "database" in lower and "locked" in lower:

            return "Database Locked"

        if "connection" in lower:

            return "Connection Issue"

        if "valueerror" in lower or "invalid" in lower:

            return "Invalid Data"

        return raw




    async def cmd_showid(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /showid command"""


        ads = list_campaigns()


        if not ads:


            await update.message.reply_text("Ad IDs\n\nNo ads yet.")


            return


        lines = ["Ad IDs"]


        for i, c in enumerate(ads, start=1):


            lines.append(f"{i}. {c.name} (id={c.id})")


        await update.message.reply_text("\n".join(lines))





    async def cmd_disabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /disabled command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /disabled <ad_id>")


            return


        ad_id = context.args[0]


        msg = await self.control.list_disabled(ad_id)


        await update.message.reply_text(msg)





    async def cmd_cleardisabled(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /cleardisabled command"""


        err = self._require_control()


        if err:


            await update.message.reply_text(err)


            return


        if not context.args:


            await update.message.reply_text("Usage: /cleardisabled <ad_id>")


            return


        ad_id = context.args[0]


        msg = await self.control.clear_disabled(ad_id)


        await update.message.reply_text(msg)





    async def cmd_admenu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):


        """Handle /admenu command (alias)"""
        await self._send_menu(chat_id=update.effective_chat.id)





    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:


        if not update.message or not update.message.text:


            return

        if update.effective_chat is None or str(update.effective_chat.type).lower() != "private":
            return

        if not self.bot:
            return
        if await self.inline_panel.try_handle_text(update, context):
            return
        await update.message.reply_text("ℹ️ Use /menu to open the control center.")







# Alert templates


async def alert_ad_started(bot: TelegramBotManager, ad_name: str):


    """Send ad started alert"""


    await bot.send_message(


        f"\U0001F680 Ad Started\n\n"


        f"Ad: {ad_name}\n"


        f"Time: {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"

    )








async def alert_ad_completed(bot: TelegramBotManager, ad_name: str, sent: int, total: int):


    """Send ad completed alert"""


    success_rate = (sent / total * 100) if total > 0 else 0


    await bot.send_message(


        f"\u2705 Ad Completed\n\n"


        f"Ad: {ad_name}\n"


        f"Sent: {sent}/{total} ({success_rate:.1f}%)\n"


        f"Time: {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"

    )








async def alert_error(bot: TelegramBotManager, ad_name: str, error: str):


    """Send error alert"""


    await bot.send_message(


        f"\u26a0\ufe0f Error Detected\n\n"


        f"Ad: {ad_name}\n"


        f"Error: {error}\n"


        f"Time: {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"

    )








async def alert_floodwait(bot: TelegramBotManager, ad_name: str, seconds: int):


    """Send FloodWait alert"""


    mins = seconds // 60


    await bot.send_message(


        f"\u23f3 FloodWait Detected\n\n"


        f"Ad: {ad_name}\n"


        f"Paused for: {mins} minutes\n"


        f"Time: {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"

    )








# Bot config storage


BOT_CONFIG_FILE = DATA_DIR / "bot_config.json"


DEFAULT_ALERT_MODE = "errors"  # "every" | "summary" | "errors"


DEFAULT_ALERT_EVERY_N = 10


DEFAULT_AUTO_START_ON_LOGIN = True








def save_bot_config(


    *,


    token: str,


    chat_id: str,


    enabled: bool = True,


    alert_mode: str = DEFAULT_ALERT_MODE,


    alert_every_n: int = DEFAULT_ALERT_EVERY_N,


    auto_start_on_login: bool = DEFAULT_AUTO_START_ON_LOGIN,


):


    """Save bot configuration"""


    save_json(BOT_CONFIG_FILE, {


        "token": token,


        "chat_id": chat_id,


        "enabled": enabled,


        "alert_mode": alert_mode,


        "alert_every_n": alert_every_n,


        "auto_start_on_login": auto_start_on_login,


    })








def load_bot_config() -> Optional[dict]:


    """Load bot configuration"""


    cfg = load_json(BOT_CONFIG_FILE, default=None)


    if not cfg:


        return None


    if "alert_mode" not in cfg:


        cfg["alert_mode"] = DEFAULT_ALERT_MODE


    if "alert_every_n" not in cfg:


        cfg["alert_every_n"] = DEFAULT_ALERT_EVERY_N


    if "auto_start_on_login" not in cfg:


        cfg["auto_start_on_login"] = DEFAULT_AUTO_START_ON_LOGIN


    return cfg








def is_bot_configured() -> bool:


    """Check if bot is configured"""


    config = load_bot_config()


    return config is not None and config.get("enabled", False)








def delete_bot_config() -> bool:


    """Delete bot configuration"""


    try:


        if BOT_CONFIG_FILE.exists():


            BOT_CONFIG_FILE.unlink()


            return True


    except Exception:


        return False


    return False
