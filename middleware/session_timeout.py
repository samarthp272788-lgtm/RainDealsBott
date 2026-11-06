import logging
import time
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from services.session_policy import GENERAL_IDLE_SECONDS, PAYMENT_IDLE_SECONDS
from services.session_resume import make_snapshot, save_resume, load_resume, saved_prompt, is_admin, without_prompt_capture

GENERAL_SESSION_TIMEOUT_SECONDS = GENERAL_IDLE_SECONDS
EXTENDED_SESSION_TIMEOUT_SECONDS = PAYMENT_IDLE_SECONDS
MAX_TRACKED_SESSIONS = 10000
logger = logging.getLogger(__name__)


def _message_timestamp(message):
    value = getattr(message, "edit_date", None) or getattr(message, "date", None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    return float(value) if isinstance(value, (int,float)) else time.time()


def _timeout_for_context(callback_data="", message_text="", state=None):
    if state:
        return PAYMENT_IDLE_SECONDS if state.startswith('DepositState:') else GENERAL_IDLE_SECONDS
    value = (callback_data or '').lower()
    return PAYMENT_IDLE_SECONDS if value.startswith(('deposit_', 'resume_deposit_')) else GENERAL_IDLE_SECONDS


class CallbackSessionTimeoutMiddleware(BaseMiddleware):
    def __init__(self):
        self._sessions = {}

    def _record_activity(self, key, timestamp, timeout):
        if len(self._sessions) >= MAX_TRACKED_SESSIONS:
            cutoff = time.time() - PAYMENT_IDLE_SECONDS
            self._sessions = {k:v for k,v in self._sessions.items() if v[0] >= cutoff}
            if len(self._sessions) >= MAX_TRACKED_SESSIONS and key not in self._sessions:
                self._sessions.pop(next(iter(self._sessions)))
        self._sessions[key] = (timestamp,timeout)

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event,CallbackQuery) else event
        if not isinstance(message,Message) or not event.from_user or message.chat.type != 'private':
            return await handler(event,data)
        key = (event.from_user.id,message.chat.id)
        state = data.get('state')
        before_state = await state.get_state() if state else None
        command = (message.text or '').split(maxsplit=1)[0].split('@')[0].lower() if isinstance(event,Message) and message.text else ''
        callback = event.data or '' if isinstance(event,CallbackQuery) else ''
        bypass = command in {'/start','/restart'} or callback in {'session_resume','session_home','main_menu'}
        now, previous = time.time(), self._sessions.get(key)
        snapshot = None
        if previous is None and not before_state and not bypass:
            try:
                snapshot = await load_resume(key)
            except Exception:
                logger.warning('Draft lookup unavailable; no saved input was executed.')
        last = previous[0] if previous else (snapshot or {}).get('last_activity')
        if last is None and isinstance(event,CallbackQuery):
            last = _message_timestamp(message)
        # The previous flow determines expiry, not the button the user is now clicking.
        timeout = _timeout_for_context(state=before_state) if before_state else (
            previous[1] if previous else _timeout_for_context(callback,state=(snapshot or {}).get('state')))
        needs_restore = bool(snapshot and snapshot.get('state') and not before_state)
        if not bypass and (needs_restore or (last is not None and now-last >= timeout)):
            if state and before_state:
                resume_data = await state.get_data()
                save_resume(key,make_snapshot(before_state,resume_data,admin=is_admin(key[0]),
                            prompt=saved_prompt(key),last_activity=last))
            if isinstance(event,CallbackQuery):
                # Restore the original popup behavior; don't replace the draft card.
                await event.answer('⏳ Session ended. /restart — Continue · /start — Home',show_alert=True)
            else:
                from utils.ui import answer_card
                with without_prompt_capture():
                    await answer_card(event,'⏳ Session ended.\n/restart — Continue, then resend your input.\n/start — Home',
                        preserve_prompt=True,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text='Continue',callback_data='session_resume'),
                            InlineKeyboardButton(text='Home',callback_data='session_home')]]))
            return None
        self._record_activity(key,now,timeout)
        result = await handler(event,data)
        current = await state.get_state() if state else None
        self._record_activity(key,now,_timeout_for_context(callback,state=current))
        if command in {'/start','/restart'} or callback in {'session_resume','session_home','main_menu'}:
            return result
        if state:
            from utils.ui import get_card_location
            resume_data = await state.get_data()
            location = get_card_location(*key)
            if location:
                resume_data.update(_card_chat_id=location[0],_card_message_id=location[1])
            try:
                save_resume(key,make_snapshot(current,resume_data,callback if isinstance(event,CallbackQuery) else None,
                            admin=is_admin(key[0]),prompt=saved_prompt(key),last_activity=now))
            except (TypeError,ValueError):
                logger.warning('Draft could not be serialized; current input remains active.')
        return result
