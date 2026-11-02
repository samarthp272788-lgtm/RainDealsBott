"""
utils/ui.py

Everything the bot shows to a user or admin lives in ONE message per
chat -- the "card". Tapping a button updates that same card instead of
sending a new message underneath it, and typing a reply (quantity,
amount, tx hash, promo code, etc.) also updates that same card instead
of spamming the chat with a fresh bubble for every step.

Two helpers cover every situation in this codebase:

- show(callback, ...)        -> use inside @router.callback_query handlers.
                                 Edits the tapped message in place.

- update_card(message, ...)  -> use inside @router.message handlers that
                                 are replying to a typed answer (quantity,
                                 deposit amount, tx hash, promo code, ...).
                                 Edits the card that was last shown via
                                 show(..., state=state), and deletes the
                                 user's input message so it doesn't linger
                                 in the chat.

Both fall back to sending a fresh message only when editing is truly
impossible (card too old / deleted / never existed yet), so nothing
ever silently fails to respond.
"""

import asyncio
import html
import logging
import re
from collections import OrderedDict
from html.parser import HTMLParser

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

# Telegram's hard cap on message text (after HTML entity parsing).
# Leave real headroom below the true 4096 limit for the truncation
# notice itself plus any small counting differences.
TELEGRAM_TEXT_LIMIT = 4096
SAFE_TEXT_LIMIT = 3900

# Keys used inside FSMContext data to remember which message is the
# "card" for a given chat, so text-reply steps know what to edit.
_CARD_CHAT_ID_KEY = "_card_chat_id"
_CARD_MESSAGE_ID_KEY = "_card_message_id"

# Lightweight fallback for handlers that don't receive an FSMContext.
# FSM data is still preferred when supplied; the cache prevents ordinary
# menus and text prompts from stacking bubbles during the process lifetime.
_CARD_LOCATIONS: OrderedDict[tuple[int, int], tuple[int, int]] = OrderedDict()
_CARD_CACHE_LIMIT = 10_000
_CARD_DELETE_TASKS: dict[tuple[int, int], asyncio.Task] = {}
_CARD_TRANSITION_TASKS: dict[tuple[int, int], asyncio.Task] = {}


def _remember_card(user_id: int, chat_id: int, message_id: int) -> None:
    key = (int(user_id), int(chat_id))
    _CARD_LOCATIONS[key] = (int(chat_id), int(message_id))
    _CARD_LOCATIONS.move_to_end(key)
    while len(_CARD_LOCATIONS) > _CARD_CACHE_LIMIT:
        _CARD_LOCATIONS.popitem(last=False)


def _forget_card(user_id: int, chat_id: int) -> None:
    _CARD_LOCATIONS.pop((int(user_id), int(chat_id)), None)


def _cancel_scheduled_card_delete(chat_id: int, message_id: int) -> None:
    key = (int(chat_id), int(message_id))
    task = _CARD_DELETE_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()


def _cancel_scheduled_card_transition(chat_id: int, message_id: int) -> None:
    key = (int(chat_id), int(message_id))
    task = _CARD_TRANSITION_TASKS.pop(key, None)
    if task and not task.done():
        task.cancel()


def schedule_card_transition(
    bot,
    *,
    chat_id: int,
    message_id: int,
    delay_seconds: int,
    text: str,
    reply_markup=None,
    parse_mode: str | None = "HTML",
):
    """Schedule a card update that is cancelled if the user navigates first."""
    key = (int(chat_id), int(message_id))
    _cancel_scheduled_card_transition(*key)
    _cancel_scheduled_card_delete(*key)

    async def _transition_later():
        try:
            await asyncio.sleep(max(1, int(delay_seconds)))
            try:
                await bot.edit_message_caption(
                    chat_id=key[0], message_id=key[1], caption=text,
                    parse_mode=parse_mode, reply_markup=reply_markup,
                )
            except Exception:
                await bot.edit_message_text(
                    chat_id=key[0], message_id=key[1], text=text,
                    parse_mode=parse_mode, reply_markup=reply_markup,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Scheduled Telegram card transition failed (%s)", type(exc).__name__)
        finally:
            current = _CARD_TRANSITION_TASKS.get(key)
            if current is asyncio.current_task():
                _CARD_TRANSITION_TASKS.pop(key, None)

    task = asyncio.create_task(_transition_later())
    _CARD_TRANSITION_TASKS[key] = task
    return task


def schedule_delete_card(message: Message, *, owner_id: int, delay_seconds: int = 900):
    """Delete a temporary card later, unless the conversation updates it first."""
    chat_id = int(message.chat.id)
    message_id = int(message.message_id)
    owner_id = int(owner_id)
    key = (chat_id, message_id)
    _cancel_scheduled_card_transition(chat_id, message_id)
    _cancel_scheduled_card_delete(chat_id, message_id)

    async def _delete_later():
        try:
            await asyncio.sleep(max(1, int(delay_seconds)))
            await message.bot.delete_message(chat_id=chat_id, message_id=message_id)
            if _CARD_LOCATIONS.get((owner_id, chat_id)) == (chat_id, message_id):
                _forget_card(owner_id, chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Temporary Telegram card cleanup failed (%s)", type(exc).__name__)
        finally:
            current = _CARD_DELETE_TASKS.get(key)
            if current is asyncio.current_task():
                _CARD_DELETE_TASKS.pop(key, None)

    task = asyncio.create_task(_delete_later())
    _CARD_DELETE_TASKS[key] = task
    return task


_SUCCESS_WORDING = re.compile(
    r"\b(?:updated|saved|added|deleted|created|replaced|set|enabled|disabled|"
    r"cleared|cancelled|canceled|revoked|imported|uploaded|removed)\b",
    re.IGNORECASE,
)
_PERSISTENT_NOTICE_WORDS = re.compile(
    r"\b(?:deposit|payment|invoice|order|purchase|delivery|delivered|refund|"
    r"credited|transaction|ticket|balance|verification|txid|utr)\b",
    re.IGNORECASE,
)


def schedule_success_notice(
    message: Message | None,
    text: str,
    *,
    owner_id: int | None = None,
) -> None:
    """Auto-remove short action confirmations, while retaining financial records."""
    if message is None:
        return
    _cancel_scheduled_card_delete(message.chat.id, message.message_id)
    owner_id = int(owner_id if owner_id is not None else message.chat.id)
    if len(text) > 650:
        return
    plain = html.unescape(re.sub(r"<[^>]*>", " ", text)).strip()
    if not plain.startswith("✅"):
        return
    protected = _PERSISTENT_NOTICE_WORDS.search(plain)
    is_order_delete_ack = bool(re.search(r"\border deleted\b", plain, re.IGNORECASE))
    if (protected and not is_order_delete_ack) or not _SUCCESS_WORDING.search(plain):
        return
    schedule_delete_card(message, owner_id=owner_id, delay_seconds=900)


class _HTMLTruncator(HTMLParser):
    """Cuts HTML-formatted text down to `limit` visible characters
    without ever leaving a tag unclosed. Text like product
    descriptions may contain real <blockquote>/<b> tags (e.g. typed
    by an admin) — a plain text[:limit] slice could sever a tag mid-
    way and either get rejected by Telegram or render broken."""

    def __init__(self, limit: int):
        super().__init__(convert_charrefs=False)
        self.limit = limit
        self.output: list[str] = []
        self.open_tags: list[str] = []
        self.count = 0
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if self.truncated:
            return
        attr_str = "".join(f' {k}="{v}"' for k, v in attrs if v is not None)
        self.output.append(f"<{tag}{attr_str}>")
        self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self.truncated:
            return
        attr_str = "".join(f' {k}="{v}"' for k, v in attrs if v is not None)
        self.output.append(f"<{tag}{attr_str}/>")

    def handle_endtag(self, tag):
        if self.truncated:
            return
        self.output.append(f"</{tag}>")
        for i in range(len(self.open_tags) - 1, -1, -1):
            if self.open_tags[i] == tag:
                del self.open_tags[i]
                break

    def handle_data(self, data):
        if self.truncated:
            return
        remaining = self.limit - self.count
        if remaining <= 0:
            self.truncated = True
            return
        if len(data) > remaining:
            self.output.append(data[:remaining])
            self.count += remaining
            self.truncated = True
        else:
            self.output.append(data)
            self.count += len(data)

    def handle_entityref(self, name):
        if self.truncated:
            return
        if self.count >= self.limit:
            self.truncated = True
            return
        self.output.append(f"&{name};")
        self.count += 1

    def handle_charref(self, name):
        if self.truncated:
            return
        if self.count >= self.limit:
            self.truncated = True
            return
        self.output.append(f"&#{name};")
        self.count += 1

    def get_result(self) -> str:
        result = "".join(self.output)
        for tag in reversed(self.open_tags):
            result += f"</{tag}>"
        return result


def truncate_html(text: str, limit: int = SAFE_TEXT_LIMIT) -> str:
    """Safely shorten HTML-formatted text to at most `limit` visible
    characters, always leaving well-formed HTML (every opened tag is
    closed). Appends a short notice when content was actually cut."""
    if len(text) <= limit:
        return text
    parser = _HTMLTruncator(limit)
    parser.feed(text)
    result = parser.get_result()
    if parser.truncated:
        result += "\n\n<i>… (message truncated — too long to display in full)</i>"
    return result


def _kwargs(reply_markup, parse_mode, extra=None):
    """Only pass parse_mode through when explicitly given, so callers
    that don't set it keep relying on the bot's default parse mode
    (HTML, configured in main.py) instead of Telegram disabling
    parsing entirely."""
    kwargs = dict(extra or {})
    kwargs["reply_markup"] = reply_markup
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    return kwargs


def _caption_kwargs(kwargs):
    """Remove text-only options before calling Telegram's caption methods."""
    caption_kwargs = dict(kwargs)
    # Telegram/Aiogram accepts link-preview controls for text messages, not
    # editMessageCaption. Keep these options on the text-edit path, but strip
    # them if update_card/show falls back to editing a photo caption.
    caption_kwargs.pop("disable_web_page_preview", None)
    caption_kwargs.pop("link_preview_options", None)
    return caption_kwargs


async def show(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode: str | None = None,
    state=None,
    replace_media: bool = False,
    **extra,
):
    """
    Single-card navigation for button taps: edits the tapped message
    in place instead of sending a new one underneath it.

    Pass state=... whenever this screen may be followed by a typed
    reply (e.g. "send the quantity", "send the amount") so that the
    follow-up message handler knows which card to keep editing.
    """
    kwargs = _kwargs(reply_markup, parse_mode, extra)
    _cancel_scheduled_card_transition(callback.message.chat.id, callback.message.message_id)
    _cancel_scheduled_card_delete(callback.message.chat.id, callback.message.message_id)

    try:
        if replace_media and callback.message.photo:
            # Telegram cannot turn a photo message into a text message via edit.
            try:
                await callback.message.delete()
            except Exception:
                pass
            msg = await callback.message.answer(text, **kwargs)
        else:
            msg = await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            msg = callback.message
        elif "message is too long" in err:
            # The text itself exceeds Telegram's 4096-char limit (e.g.
            # a long product description). Truncate safely (keeping
            # HTML well-formed) and retry instead of repeating the
            # exact failure on the fallback send below.
            logger.warning("show(): message too long (%d chars), truncating", len(text))
            safe_text = truncate_html(text)
            safe_kwargs = _kwargs(reply_markup, parse_mode, extra)
            try:
                msg = await callback.message.edit_text(safe_text, **safe_kwargs)
            except TelegramBadRequest as e2:
                logger.info("show(): edit_text failed after truncation (%s), sending a new message", e2)
                msg = await callback.message.answer(safe_text, **safe_kwargs)
        else:
            # Photo-based cards (such as deposit QR cards) have a caption,
            # not text. Edit that caption in place before creating a second
            # message, so navigation and status updates stay on one card.
            try:
                msg = await callback.message.edit_caption(
                    caption=text,
                    **_caption_kwargs(kwargs),
                )
            except TelegramBadRequest as caption_error:
                if "message is not modified" in str(caption_error).lower():
                    msg = callback.message
                else:
                    logger.info(
                        "show(): text/caption edit failed (%s / %s), sending a new message",
                        e,
                        caption_error,
                    )
                    msg = await callback.message.answer(text, **kwargs)

    _remember_card(callback.from_user.id, msg.chat.id, msg.message_id)
    _cancel_scheduled_card_delete(msg.chat.id, msg.message_id)
    schedule_success_notice(msg, text, owner_id=callback.from_user.id)
    if state is not None:
        await state.update_data(
            **{_CARD_CHAT_ID_KEY: msg.chat.id, _CARD_MESSAGE_ID_KEY: msg.message_id}
        )

    return msg


async def update_card(
    message: Message,
    state=None,
    text: str = "",
    reply_markup=None,
    parse_mode: str | None = None,
    delete_input: bool = True,
    chat_id: int | None = None,
    message_id: int | None = None,
    owner_id: int | None = None,
    replace_media: bool = False,
    **extra,
):
    """
    Single-card navigation for typed replies: edits the same card that
    show() last displayed instead of sending a new message, and
    deletes the user's input message so the chat stays a single,
    continuously-updating card.

    - state: FSMContext to read the tracked card id from (and to keep
      tracking it going forward). Pass None if you already have the
      exact chat_id/message_id to edit (e.g. you captured them earlier
      in the same handler, before clearing the state).
    - chat_id/message_id: explicit override, used instead of / in
      addition to whatever is in state.

    Falls back to sending (and starting to track) a brand new card if
    there's nothing to edit yet, or the old card can't be edited
    anymore.
    """
    if delete_input:
        try:
            await message.delete()
        except Exception:
            pass

    if (chat_id is None or message_id is None) and state is not None:
        data = await state.get_data()
        chat_id = chat_id or data.get(_CARD_CHAT_ID_KEY)
        message_id = message_id or data.get(_CARD_MESSAGE_ID_KEY)

    card_owner_id = int(owner_id or (message.from_user.id if message.from_user else 0))
    if (chat_id is None or message_id is None) and card_owner_id:
        remembered = _CARD_LOCATIONS.get((card_owner_id, message.chat.id))
        if remembered:
            chat_id = chat_id or remembered[0]
            message_id = message_id or remembered[1]

    if chat_id and message_id and replace_media:
        try:
            await message.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        msg = await message.answer(text, **_kwargs(reply_markup, parse_mode, extra))
        if state is not None:
            await state.update_data(
                **{_CARD_CHAT_ID_KEY: msg.chat.id, _CARD_MESSAGE_ID_KEY: msg.message_id}
            )
        if card_owner_id:
            _remember_card(card_owner_id, msg.chat.id, msg.message_id)
            schedule_success_notice(msg, text, owner_id=card_owner_id)
        return msg

    if chat_id and message_id:
        _cancel_scheduled_card_transition(chat_id, message_id)

    kwargs = _kwargs(reply_markup, parse_mode, extra)
    if chat_id and message_id:
        try:
            msg = await message.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, **kwargs
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                _cancel_scheduled_card_delete(chat_id, message_id)
                return None
            if "message is too long" in str(exc).lower():
                logger.warning("update_card(): message too long (%d chars), truncating", len(text))
                text = truncate_html(text)
                try:
                    msg = await message.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, **kwargs
                    )
                except TelegramBadRequest as retry_exc:
                    logger.info("update_card(): edit failed after truncation (%s)", retry_exc)
                    msg = None
            else:
                # A photo card uses its caption as the editable text.
                try:
                    msg = await message.bot.edit_message_caption(
                        caption=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        **_caption_kwargs(kwargs),
                    )
                except TelegramBadRequest as caption_exc:
                    if "message is not modified" in str(caption_exc).lower():
                        _cancel_scheduled_card_delete(chat_id, message_id)
                        return None
                    logger.info(
                        "update_card(): text/caption edit failed (%s / %s)",
                        exc,
                        caption_exc,
                    )
                    msg = None
        if msg is not None:
            _cancel_scheduled_card_delete(msg.chat.id, msg.message_id)
            if state is not None:
                await state.update_data(
                    **{_CARD_CHAT_ID_KEY: msg.chat.id, _CARD_MESSAGE_ID_KEY: msg.message_id}
                )
            if card_owner_id:
                _remember_card(card_owner_id, msg.chat.id, msg.message_id)
                schedule_success_notice(msg, text, owner_id=card_owner_id)
            return msg

    msg = await message.answer(text, **kwargs)
    if state is not None:
        await state.update_data(
            **{_CARD_CHAT_ID_KEY: msg.chat.id, _CARD_MESSAGE_ID_KEY: msg.message_id}
        )
    if card_owner_id:
        _remember_card(card_owner_id, msg.chat.id, msg.message_id)
        schedule_success_notice(msg, text, owner_id=card_owner_id)
    return msg


async def answer_card(
    target,
    text: str | None = None,
    *args,
    state=None,
    reply_markup=None,
    parse_mode: str | None = None,
    delete_input: bool = True,
    **kwargs,
):
    """Replace a conversation's current card for Message or CallbackQuery replies.

    Callback acknowledgements with no text remain ephemeral Telegram popups.
    Actual text replies edit the tracked card, falling back to a new message.
    """
    if isinstance(target, CallbackQuery):
        if text is None:
            return await target.answer(*args, **kwargs)
        return await show(
            target,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            state=state,
            **kwargs,
        )

    if isinstance(target, Message):
        if text is None:
            return await target.answer(*args, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
        return await update_card(
            target,
            state=state,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            delete_input=delete_input,
            **kwargs,
        )

    raise TypeError("answer_card expects an aiogram Message or CallbackQuery")
