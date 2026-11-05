import time
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from services.session_resume import make_snapshot, save_resume


GENERAL_SESSION_TIMEOUT_SECONDS = 7 * 60
EXTENDED_SESSION_TIMEOUT_SECONDS = 7 * 60
MAX_TRACKED_SESSIONS = 10000


def _message_timestamp(message) -> float:
    value = getattr(message, "edit_date", None) or getattr(message, "date", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return time.time()


def _timeout_for_context(callback_data: str = "", message_text: str = "") -> int:
    context = f"{callback_data} {message_text}".lower()
    extended_terms = (
        "deposit", "payment", "bep20", "polygon", "binance", "upi", "usdt",
        "support", "ticket", "checkout", "purchase", "confirm order",
    )
    if any(term in context for term in extended_terms):
        return EXTENDED_SESSION_TIMEOUT_SECONDS
    return GENERAL_SESSION_TIMEOUT_SECONDS


class CallbackSessionTimeoutMiddleware(BaseMiddleware):
    def __init__(self):
        self._sessions: dict[tuple[int, int], tuple[float, int]] = {}

    def _record_activity(self, key: tuple[int, int], timestamp: float, timeout: int) -> None:
        if len(self._sessions) >= MAX_TRACKED_SESSIONS:
            cutoff = time.time() - EXTENDED_SESSION_TIMEOUT_SECONDS
            self._sessions = {
                session_key: session
                for session_key, session in self._sessions.items()
                if session[0] >= cutoff
            }
        self._sessions[key] = (timestamp, timeout)

    async def __call__(self, handler, event, data):
        state = data.get('state')
        before_state = await state.get_state() if state else None
        if isinstance(event, CallbackQuery) and event.message:
            key = (event.from_user.id, event.message.chat.id)
            now = time.time()
            message_timestamp = _message_timestamp(event.message)
            message_text = getattr(event.message, "text", None) or getattr(
                event.message, "caption", ""
            )
            timeout = _timeout_for_context(event.data or "", message_text)
            previous = self._sessions.get(key)
            last_activity = max(message_timestamp, previous[0] if previous else 0)

            if now - last_activity > timeout and event.data not in {'session_resume', 'session_home', 'main_menu'}:
                await event.answer(
                    "Session paused. /restart continues · /start opens home.",
                    show_alert=True,
                )
                from utils.ui import show
                await show(event, 'Session paused.\n/restart — Continue\n/start — Home',
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                               InlineKeyboardButton(text='Continue', callback_data='session_resume'),
                               InlineKeyboardButton(text='Home', callback_data='session_home'),
                           ]]))
                return None

            self._record_activity(key, now, timeout)
        elif isinstance(event, Message) and event.from_user:
            key = (event.from_user.id, event.chat.id)
            previous = self._sessions.get(key)
            message_text = event.text or event.caption or ""
            timeout = previous[1] if previous else _timeout_for_context(message_text=message_text)
            command = message_text.split(maxsplit=1)[0].split('@')[0].lower() if message_text else ''
            if previous and before_state and time.time() - previous[0] > timeout and command not in {'/start','/restart'}:
                from utils.ui import answer_card
                await answer_card(event, 'Session paused.\n/restart — Continue, then resend your input.\n/start — Home')
                return None
            self._record_activity(key, _message_timestamp(event), timeout)

        result = await handler(event, data)
        if state and getattr(event, 'from_user', None):
            message = event.message if isinstance(event, CallbackQuery) else event
            if message and message.chat.type == 'private':
                current = await state.get_state()
                if isinstance(event, CallbackQuery) or before_state or current:
                    navigation = event.data if isinstance(event, CallbackQuery) else None
                    if navigation not in {'session_resume', 'session_home'}:
                        from utils.ui import get_card_location
                        resume_data = await state.get_data()
                        location = get_card_location(event.from_user.id, message.chat.id)
                        if location:
                            resume_data = {**resume_data, '_card_chat_id': location[0], '_card_message_id': location[1]}
                        save_resume((event.from_user.id, message.chat.id),
                                    make_snapshot(current, resume_data, navigation))
        return result
