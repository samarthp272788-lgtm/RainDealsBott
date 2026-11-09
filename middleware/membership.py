import logging
import asyncio
import re
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject, Update

from config import (
    ADMIN_IDS,
    BANNED_USER_CACHE_TTL,
    CHANNEL_LINK,
    GROUP_LINK,
    ADDITIONAL_CHANNEL_LINK,
    ADDITIONAL_CHANNEL_ID,
    MEMBERSHIP_CACHE_TTL,
    MEMBERSHIP_RETRY_DELAY_SECONDS,
    MEMBERSHIP_VERIFY_ATTEMPTS,
    GROUP_ID,
    ORDER_NOTIFICATION_CHANNEL_ID,
    STOCK_NOTIFICATION_CHANNEL_ID,
    ADMIN_ORDER_NOTIFICATION_CHANNEL_ID,
    NOTIFICATION_CHANNEL_ID,
    STOCK_GROUP_ID,
)
from database import SessionLocal
from models.user import User
from services.maintenance_mode import is_maintenance_enabled

logger = logging.getLogger(__name__)


def _extract_username(link: str) -> str | None:
    """Extract @username from a public t.me link; skip private invites."""
    if not link:
        return None

    username = link.rstrip("/").split("/")[-1]
    if username.startswith("+"):
        logger.warning("Skipping private membership link: %s", link)
        return None

    return f"@{username.lstrip('@')}"


CHANNEL_USERNAME = _extract_username(CHANNEL_LINK)
GROUP_USERNAME = _extract_username(GROUP_LINK)
ADDITIONAL_CHANNEL_CHAT_ID = ADDITIONAL_CHANNEL_ID or _extract_username(ADDITIONAL_CHANNEL_LINK)


def _chat_matches_target(chat, target: Any) -> bool:
    """Match a configured numeric group id or public @username to a chat."""
    if target is None or not str(target).strip():
        return False
    value = str(target).strip()
    if value.lstrip("-").isdigit():
        try:
            return int(value) == int(chat.id)
        except (TypeError, ValueError):
            return False
    username = value.rsplit("/", 1)[-1].lstrip("@").lower()
    return bool(username and getattr(chat, "username", None) and username == chat.username.lower())


def is_order_notification_chat(chat) -> bool:
    """Notification destinations are output-only; user traffic stays in DMs."""
    targets = (
        GROUP_ID,
        CHANNEL_USERNAME,
        GROUP_USERNAME,
        ORDER_NOTIFICATION_CHANNEL_ID,
        STOCK_NOTIFICATION_CHANNEL_ID,
        ADMIN_ORDER_NOTIFICATION_CHANNEL_ID,
        NOTIFICATION_CHANNEL_ID,
        STOCK_GROUP_ID,
    )
    return any(_chat_matches_target(chat, target) for target in targets)


async def _delete_group_message(message: Message) -> None:
    try:
        await message.delete()
    except Exception as error:
        logger.info(
            "Could not remove message from notification-only chat %s; bot needs delete-message permission (%s)",
            message.chat.id,
            type(error).__name__,
        )


async def check_user_membership(
    bot,
    user_id: int,
    *,
    force_refresh: bool = False,
    fast: bool = False,
    with_details: bool = False,
) -> bool | tuple[bool, list[str]]:
    """Return True only when the user belongs to every configured public chat.

    A failed result is never cached: a user may have just joined and Telegram
    can need a few seconds before getChatMember reflects that change.
    """
    now = time.monotonic()
    cached = _membership_cache.get(user_id)
    if not force_refresh and cached and now - cached[1] < MEMBERSHIP_CACHE_TTL:
        return (cached[0], []) if with_details else cached[0]

    chats = [
        ("RainOrdersGroup", CHANNEL_USERNAME),
        ("RainStockGroup", GROUP_USERNAME),
        ("RainWorld channel", ADDITIONAL_CHANNEL_CHAT_ID),
    ]
    chats = [(name, chat_id) for name, chat_id in chats if chat_id]
    if not chats:
        logger.warning("No valid public membership chat configured; allowing access")
        return (True, []) if with_details else True

    async def check_chat(name: str, chat_id: str | int) -> tuple[bool, str | None]:
        attempts = 1 if fast else MEMBERSHIP_VERIFY_ATTEMPTS
        timeout = 3 if fast else 5
        for attempt in range(attempts):
            try:
                # Check chats concurrently. A recently joined user is checked
                # again briefly because Telegram membership updates can lag.
                member = await asyncio.wait_for(
                    bot.get_chat_member(chat_id=chat_id, user_id=user_id), timeout=timeout
                )
                if member.status not in ("left", "kicked") and not (
                    member.status == "restricted" and not getattr(member, "is_member", False)
                ):
                    return True, None
                logger.info("Membership missing: user=%s chat=%s status=%s", user_id, name, member.status)
                return False, name
            except Exception as error:
                if "member list is inaccessible" in str(error).lower():
                    logger.error("Bot cannot inspect membership for %s (%s); add it as an administrator", name, chat_id)
                    return False, f"{name} (bot needs admin access)"
                logger.warning(
                    "Membership verification attempt %s/%s failed for %s (%s): %s",
                    attempt + 1,
                    attempts,
                    name,
                    chat_id,
                    type(error).__name__,
                )

            if attempt < attempts - 1:
                await asyncio.sleep(MEMBERSHIP_RETRY_DELAY_SECONDS)
            else:
                return False, f"{name} (verification failed)"

        return False, name

    results = await asyncio.gather(*(check_chat(name, chat_id) for name, chat_id in chats))
    failures = [failure for _, failure in results if failure]
    is_member = all(is_member for is_member, _ in results)
    if is_member and MEMBERSHIP_CACHE_TTL > 0:
        _membership_cache[user_id] = (is_member, now)
    else:
        _membership_cache.pop(user_id, None)
    return (is_member, failures) if with_details else is_member


def get_join_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    if CHANNEL_LINK and not CHANNEL_LINK.rstrip("/").split("/")[-1].startswith("+"):
        buttons.append([InlineKeyboardButton(text="📢 Join Channel", url=CHANNEL_LINK)])
    if GROUP_LINK and not GROUP_LINK.rstrip("/").split("/")[-1].startswith("+"):
        buttons.append([InlineKeyboardButton(text="👥 Join Group", url=GROUP_LINK)])
    if ADDITIONAL_CHANNEL_LINK and not ADDITIONAL_CHANNEL_LINK.rstrip("/").split("/")[-1].startswith("+"):
        buttons.append([InlineKeyboardButton(text="📣 Join RainWorld Channel", url=ADDITIONAL_CHANNEL_LINK)])
    buttons.append([InlineKeyboardButton(text="🔄 Try Again", callback_data="check_membership_retry")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def is_user_banned(telegram_id: int) -> bool:
    """Read the ban flag for a Telegram user. Admins are never restricted."""
    if telegram_id in ADMIN_IDS:
        return False

    db = SessionLocal()
    try:
        user = db.query(User.is_banned).filter(User.telegram_id == telegram_id).first()
        return bool(user and user[0])
    finally:
        db.close()


# Membership and ban status do not change on every update. Keeping these
# caches local is safe for responsiveness; ban cache duration is deliberately
# short so administration changes become effective quickly.
_membership_cache: dict[int, tuple[bool, float]] = {}
_banned_user_cache: dict[int, tuple[bool, float]] = {}


def _read_banned_user_cached(telegram_id: int) -> bool:
    now = time.monotonic()
    cached = _banned_user_cache.get(telegram_id)
    if cached and now - cached[1] < BANNED_USER_CACHE_TTL:
        return cached[0]

    banned = is_user_banned(telegram_id)
    if BANNED_USER_CACHE_TTL > 0:
        _banned_user_cache[telegram_id] = (banned, now)
    return banned


# These are the ONLY exact callback values and anchored read-only patterns
# a banned user can use. Never add a broad family prefix here.
BANNED_READ_ONLY_CALLBACKS = frozenset({
    "orders_menu",
    "orders_view_all",
    "support_menu",
    "support_ticket",
    "support_my_tickets",
    "contact_info",
    "my_deposits",
})
BANNED_READ_ONLY_CALLBACK_PATTERNS = (
    re.compile(r"order_(?:detail|receipt|share)_\d+\Z"),
    re.compile(r"deposits_page_\d+\Z"),
)

# Prevents repeated restricted-account messages when a banned user types.
_restricted_menu_shown: set[int] = set()


def get_restricted_keyboard() -> InlineKeyboardMarkup:
    """The only navigation a restricted account is allowed to see."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 My Orders", callback_data="orders_menu")],
        [InlineKeyboardButton(text="🎫 Support Tickets", callback_data="support_ticket")],
        [InlineKeyboardButton(text="📥 My Deposits", callback_data="my_deposits")],
    ])


RESTRICTED_TEXT = (
    "🚫 <b>Account Restricted</b>\n\n"
    "Your account cannot make purchases or deposits.\n"
    "You can still view your existing orders, support tickets, and deposits."
)

MAINTENANCE_TEXT = "🛠 <b>Rain Store is currently under maintenance.</b>\n\nPlease check back soon."
MEMBERSHIP_REQUIRED_TEXT = (
    "🔒 <b>Join all required channels to use the bot.</b>\n\n"
    "After joining, tap <b>Try Again</b> to verify your membership."
)


def _is_banned_read_only_callback(callback_data: str | None) -> bool:
    if not callback_data:
        return False
    return callback_data in BANNED_READ_ONLY_CALLBACKS or any(
        pattern.fullmatch(callback_data) for pattern in BANNED_READ_ONLY_CALLBACK_PATTERNS
    )


class BannedUserMiddleware(BaseMiddleware):
    """Block banned accounts before handlers run.

    Banned users may only use callback buttons listed in
    BANNED_READ_ONLY_CALLBACKS. All messages are blocked, preventing
    purchases, deposits, and support-ticket replies by default.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Dispatcher-level middleware receives an Update wrapper. Router-level
        # middleware receives the Message/CallbackQuery itself.
        actual_event = event.event if isinstance(event, Update) else event
        from_user = getattr(actual_event, "from_user", None)

        # Order/stock destinations are output-only. Remove Telegram join/leave
        # service bubbles and non-admin chatter there, and never answer with a
        # membership prompt in a notification group.
        if isinstance(actual_event, Message) and actual_event.chat.type != "private":
            if is_order_notification_chat(actual_event.chat):
                is_join_leave = bool(
                    actual_event.new_chat_members or actual_event.left_chat_member
                )
                if is_join_leave or (from_user and from_user.id not in ADMIN_IDS):
                    await _delete_group_message(actual_event)
                return None

        if from_user and from_user.id not in ADMIN_IDS and await asyncio.to_thread(is_maintenance_enabled):
            if isinstance(actual_event, CallbackQuery):
                if actual_event.message:
                    try:
                        await actual_event.message.edit_text(MAINTENANCE_TEXT, parse_mode="HTML")
                    except Exception:
                        await actual_event.answer(MAINTENANCE_TEXT, show_alert=True)
                else:
                    await actual_event.answer(MAINTENANCE_TEXT, show_alert=True)
            elif isinstance(actual_event, Message):
                try:
                    await actual_event.delete()
                except Exception:
                    pass
                await actual_event.answer(MAINTENANCE_TEXT, parse_mode="HTML")
            return None

        private_chat_event = (
            isinstance(actual_event, Message)
            and actual_event.chat.type == "private"
        ) or (
            isinstance(actual_event, CallbackQuery)
            and actual_event.message is not None
            and actual_event.message.chat.type == "private"
        )
        if from_user and from_user.id not in ADMIN_IDS and private_chat_event:
            is_membership_retry = (
                isinstance(actual_event, CallbackQuery)
                and actual_event.data == "check_membership_retry"
            )
            if not is_membership_retry:
                membership_result = await check_user_membership(
                    data.get("bot"), from_user.id, fast=True
                )
                if not membership_result:
                    if isinstance(actual_event, CallbackQuery):
                        try:
                            if actual_event.message:
                                await actual_event.message.edit_text(
                                    MEMBERSHIP_REQUIRED_TEXT,
                                    parse_mode="HTML",
                                    reply_markup=get_join_keyboard(),
                                )
                        except Exception:
                            pass
                        await actual_event.answer("Join all required channels first.", show_alert=True)
                    elif isinstance(actual_event, Message):
                        await actual_event.answer(
                            MEMBERSHIP_REQUIRED_TEXT,
                            parse_mode="HTML",
                            reply_markup=get_join_keyboard(),
                        )
                    return None

        # SQLAlchemy/PyMySQL is synchronous. Never run it directly on the
        # asyncio event loop, otherwise one slow TiDB request freezes every
        # command and callback for all users.
        if not from_user or not await asyncio.to_thread(_read_banned_user_cached, from_user.id):
            return await handler(event, data)

        if isinstance(actual_event, CallbackQuery) and _is_banned_read_only_callback(actual_event.data):
            return await handler(event, data)

        if isinstance(actual_event, CallbackQuery):
            # Edit the existing screen instead of sending repeated messages.
            if actual_event.message:
                try:
                    await actual_event.message.edit_text(
                        RESTRICTED_TEXT,
                        parse_mode="HTML",
                        reply_markup=get_restricted_keyboard(),
                    )
                except Exception:
                    # Telegram rejects an edit when the screen is already identical.
                    pass
            await actual_event.answer("Account restricted.")
        elif isinstance(actual_event, Message):
            # Delete new typed messages, so the chat is not flooded.
            try:
                await actual_event.delete()
            except Exception:
                pass

            # Send the restricted menu just once per bot process.
            if from_user.id not in _restricted_menu_shown:
                _restricted_menu_shown.add(from_user.id)
                await actual_event.answer(
                    RESTRICTED_TEXT,
                    parse_mode="HTML",
                    reply_markup=get_restricted_keyboard(),
                )

        return None
