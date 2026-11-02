import time
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


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

            if now - last_activity > timeout:
                self._sessions.pop(key, None)
                await event.answer(
                    "⏳ Session ended. Send /start to open the menu again.",
                    show_alert=True,
                )
                return None

            self._record_activity(key, now, timeout)
        elif isinstance(event, Message) and event.from_user:
            key = (event.from_user.id, event.chat.id)
            previous = self._sessions.get(key)
            message_text = event.text or event.caption or ""
            timeout = previous[1] if previous else _timeout_for_context(message_text=message_text)
            self._record_activity(key, _message_timestamp(event), timeout)

        return await handler(event, data)
