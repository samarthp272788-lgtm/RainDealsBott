# handlers/start.py — FULLY FIXED + MAX SPEED + EXACT WALLET BALANCE

import asyncio
import logging
import uuid
from types import SimpleNamespace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape as html_escape

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from database import SessionLocal, transaction, retry_on_write_conflict
from models.user import User
from config import ADMIN_IDS, CHANNEL_LINK, GROUP_LINK, TOS_LINK

from keyboards.menu import get_main_menu, get_admin_main_menu
from utils.ui import show, answer_card

# Membership check
from middleware.membership import check_user_membership, get_join_keyboard

logger = logging.getLogger(__name__)

router = Router()
_bot_username: str | None = None
_bot_username_lock = asyncio.Lock()


async def _get_bot_username(bot) -> str:
    global _bot_username
    if not _bot_username:
        async with _bot_username_lock:
            if not _bot_username:
                bot_info = await bot.get_me()
                _bot_username = bot_info.username or ""
    return _bot_username


# ╔══════════════════════════════════════════════════════════════╗
# ║  HTML-SAFE HELPERS                                         ║
# ╚══════════════════════════════════════════════════════════════╝

def safe(text: str) -> str:
    return html_escape(str(text), quote=False)


def link(text: str, url: str) -> str:
    return f"<a href='{url}'>{safe(text)}</a>"


# ╔══════════════════════════════════════════════════════════════╗
# ║  PRE-COMPUTED STATIC STRINGS                                ║
# ╚══════════════════════════════════════════════════════════════╝

_DOUBLE_LINE = "════════════════════════════"
_SINGLE_LINE = "────────────────────────────"
_DASH_LINE = "━━━━━━━━━━━━━━━━━━━━━━"

_BOX_WELCOME = (
    "╔════════════════════════════╗\n"
    "║        🎉 WELCOME          ║\n"
    "╚════════════════════════════╝"
)

_NEW_USER_GUIDE = (
    f"{_DOUBLE_LINE}\n\n"
    f"🎯 <b>Your Account is Ready!</b>\n\n"
    f"<b>📋 Quick Start Guide:</b>\n\n"
    f"💰 <b>1. Deposit Funds</b>\n"
    f"   └ Add balance via crypto or UPI\n\n"
    f"🛍 <b>2. Browse Products</b>\n"
    f"   └ Explore our catalog\n\n"
    f"🛒 <b>3. Make a Purchase</b>\n"
    f"   └ Instant delivery for auto products\n\n"
    f"🤝 <b>4. Refer Friends</b>\n"
    f"   └ Earn commission on their purchases\n"
)

_NEW_REF_BONUS = (
    f"\n{_DOUBLE_LINE}\n\n"
    f"🎁 <b>You joined via a referral link!</b>\n"
    f"Your referrer will earn commission on your first\n"
    f"purchases. You too can share your own referral\n"
    f"link to earn rewards! 🤝"
)


# ╔══════════════════════════════════════════════════════════════╗
# ║  FAST HELPERS                                              ║
# ╚══════════════════════════════════════════════════════════════╝

def _time_greeting() -> tuple[str, str]:
    h = datetime.now().hour
    if h < 12:
        return ("🌅", "Good Morning")
    if h < 17:
        return ("☀️", "Good Afternoon")
    if h < 21:
        return ("🌆", "Good Evening")
    return ("🌙", "Good Night")


def _status(balance: float) -> str:
    if balance <= 0:
        return "Empty"
    if balance < 10:
        return "Low"
    if balance < 100:
        return "Active"
    return "Premium"


def _get_member_since(user) -> str:
    created = (
            getattr(user, 'created_at', None) or
            getattr(user, 'date_joined', None) or
            getattr(user, 'created', None) or
            getattr(user, 'joined_at', None) or
            getattr(user, 'registered_at', None)
    )
    if created is not None and hasattr(created, 'strftime'):
        return created.strftime("%d %b %Y")
    return "N/A"


def generate_ref_code() -> str:
    return uuid.uuid4().hex[:8].upper()


def _format_balance(raw_balance) -> str:
    """Display wallet values with at most three decimal places."""
    try:
        dec_val = Decimal(str(raw_balance or 0))
        rounded = dec_val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        return format(rounded, "f").rstrip("0").rstrip(".") or "0"
    except Exception:
        return "0"


# ╔══════════════════════════════════════════════════════════════╗
# ║  AUDIT-STYLE PROFILE BUILDER                               ║
# ╚══════════════════════════════════════════════════════════════╝

def _build_audit_profile(user, telegram_id: int) -> str:
    """Build the branded marketplace profile card."""
    balance_raw = getattr(user, 'balance_display', getattr(user, 'balance', 0))
    balance_str = _format_balance(balance_raw)
    total_orders = int(getattr(user, 'total_orders', 0) or 0)
    rewards_str = _format_balance(getattr(user, 'referral_earnings_display', 0) or 0)
    full_name = str(getattr(user, 'full_name', '') or 'there')
    first_name = safe(full_name.split()[0])

    return (
        "🛍 <b>Rain Store</b>\n"
        "Premium Digital Marketplace\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 Welcome back, <b>{first_name}</b>\n\n"
        "💎 Standard Plan\n\n"
        f"💰 Wallet: ${balance_str}\n"
        f"📦 Orders: {total_orders}\n"
        f"🎁 Rewards: ${rewards_str}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<blockquote>"
        "✓ Verified Premium Products\n"
        "✓ Instant Delivery\n"
        "✓ Secure Payments\n"
        "✓ Dedicated Customer Support"
        "</blockquote>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<blockquote>"
        "🛒 Shop — Browse premium digital products\n"
        "💰 Deposit — Top up your wallet instantly\n"
        "👤 Profile — Manage your account &amp; wallet\n"
        "📦 Orders — View purchases &amp; product keys\n"
        "📞 Support — Get help from our support team"
        "</blockquote>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📢 Stay Updated: @Raindrop7707\n\n"
        "👇 Tap a button below to get started."
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║  MODERN /start BUILDER — TERMINAL STYLE                     ║
# ╚══════════════════════════════════════════════════════════════╝

def _build_start_welcome(
    user, full_name: str, telegram_id: int, username: str | None,
    bot_username: str,
) -> str:
    balance = _format_balance(getattr(user, "balance_display", getattr(user, "balance", 0)))
    spent = _format_balance(getattr(user, "total_spent_display", getattr(user, "total_spent", 0)) or 0)
    earnings = _format_balance(
        getattr(user, "referral_earnings_display", getattr(user, "referral_earnings", 0)) or 0
    )
    referral_count = int(getattr(user, "total_referrals", 0) or 0)
    referral_code = str(getattr(user, "referral_code", "") or "").strip()
    referral_url = f"https://t.me/{bot_username}?start=ref_{referral_code}" if bot_username and referral_code else ""
    referral_link = f'<a href="{safe(referral_url)}">{safe(referral_url)}</a>' if referral_url else "Unavailable"
    username_text = f"@{safe(username.lstrip('@'))}" if username else "Not set"
    first_name = safe((full_name or "there").split()[0])
    return (
        "🛍 <b>RainDeals</b>\n"
        "Quality digital products at great rates\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Welcome back, <b>{first_name}</b>!\n"
        f"🏷️ Username: {username_text}\n"
        f"🆔 User ID: <code>{telegram_id}</code>\n"
        "👑 Membership: 🥉 Bronze\n"
        f"💰 Balance: ${balance}\n"
        f"💎 Total Spent: ${spent}\n"
        f"🤝 Referrals: {referral_count}\n"
        f"💸 Referral Earnings: ${earnings}\n"
        f"🔗 Referral Link: {referral_link}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Choose an option below to get started.</i>"
    )


def _load_main_menu_text(
    telegram_id: int,
    full_name: str,
    username: str | None,
    bot_username: str,
) -> str | None:
    """Load home-menu data without blocking aiogram's event loop."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return None
        return _build_start_welcome(user, full_name, telegram_id, username, bot_username)
    finally:
        db.close()


def _load_profile_text(telegram_id: int) -> str | None:
    """Load profile data in a worker thread, keeping remote DB waits off-loop."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        return _build_audit_profile(user, telegram_id) if user else None
    finally:
        db.close()


# ╔══════════════════════════════════════════════════════════════╗
# ║  CALLBACK HANDLERS                                         ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "profile_back")
async def profile_back_cb(callback: CallbackQuery):
    """Back button from Profile → DELETES the profile message. Main menu stays."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "clear_start_menu")
async def clear_start_menu(callback: CallbackQuery):
    """Delete only the home-menu message containing the tapped button."""
    try:
        if callback.message:
            await callback.message.delete()
            await callback.answer("Menu cleared.")
        else:
            await callback.answer("This menu can no longer be cleared.", show_alert=True)
    except Exception:
        await callback.answer("Could not clear this menu. Please try again.", show_alert=True)


@router.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery, state: FSMContext):
    """Back to main menu — edits the current message."""
    await state.clear()
    await callback.answer()

    bot_username = await _get_bot_username(callback.bot)
    text = await asyncio.to_thread(
        _load_main_menu_text,
        callback.from_user.id,
        callback.from_user.full_name,
        callback.from_user.username,
        bot_username,
    )
    if text is None:
        await answer_card(callback, "User record not found. Send /start to register again.")
        return

    keyboard = get_admin_main_menu() if callback.from_user.id in ADMIN_IDS else get_main_menu()
    await show(
        callback,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        replace_media=bool(callback.message.photo),
    )


@router.callback_query(F.data == "my_profile")
async def my_profile(callback: CallbackQuery):
    """Profile → sends NEW audit-style message with Back button. Main menu stays."""
    await callback.answer()

    text = await asyncio.to_thread(_load_profile_text, callback.from_user.id)
    if not text:
        await answer_card(callback, "User record not found. Send /start to register again.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back", callback_data="profile_back")],
        ]
    )
    await answer_card(callback,
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║  MEMBERSHIP RETRY CALLBACK                                 ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "check_membership_retry")
async def retry_membership_check(callback: CallbackQuery):
    """Run a fresh check after a user joins a required chat."""
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Open the bot in a private chat to verify membership.", show_alert=True)
        return

    is_member = await check_user_membership(
        callback.bot,
        callback.from_user.id,
        force_refresh=True,
        with_details=True,
    )

    is_member, failures = is_member
    if is_member:
        await callback.message.delete()
        await answer_card(callback,
            "✅ <b>Verification Successful!</b>\n\n"
            "Send /start to begin using the bot.",
            parse_mode="HTML"
        )
        await callback.answer("✅ Verified! Send /start", show_alert=True)
    else:
        await callback.answer(
            "❌ Could not verify: " + ", ".join(failures) + ". If joined, ask an admin to add the bot as an admin in that chat, then retry.",
            show_alert=True
        )


# ╔══════════════════════════════════════════════════════════════╗
# ║  USER CREATION                                             ║
# ╚══════════════════════════════════════════════════════════════╝

@retry_on_write_conflict(max_attempts=3)
def _get_or_create_user(telegram_id: int, username: str, full_name: str, ref_payload: str) -> dict:
    with transaction() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        is_new = False
        referral_bonus = False

        if not user:
            is_new = True
            referred_by = None

            if ref_payload:
                referral_code = ref_payload.removeprefix("ref_")
                referrer = (
                    db.query(User)
                    .filter(User.referral_code == referral_code)
                    .with_for_update()
                    .first()
                )
                if referrer is not None and referrer.telegram_id != telegram_id:
                    referred_by = referrer.telegram_id
                    referrer.total_referrals = (referrer.total_referrals or 0) + 1
                    referral_bonus = True

            user = User(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                balance=0,
                referral_code=generate_ref_code(),
                referred_by=referred_by,
                total_referrals=0,
                referral_earnings=0,
                total_orders=0,
                total_spent=0,
                total_deposited=0,
                is_banned=False,
            )
            db.add(user)
            db.flush()
        else:
            user.username = username
            user.full_name = full_name

        return {
            "is_new": is_new,
            "referral_bonus": referral_bonus,
            "referral_code": user.referral_code,
            "referred_by": getattr(user, "referred_by", None),
            # Return the dashboard values from the same transaction. This
            # removes the second remote database query from every /start.
            "profile": {
                "balance": user.balance,
                "total_orders": user.total_orders,
                "total_referrals": user.total_referrals,
                "total_spent": user.total_spent,
                "referral_earnings": user.referral_earnings,
                "referral_code": user.referral_code,
            },
        }


# ╔══════════════════════════════════════════════════════════════╗
# ║  /start COMMAND — With Membership Check                     ║
# ╚══════════════════════════════════════════════════════════════╝

@router.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject):
    # This bot's user flow is private-chat only. In particular, never expose
    # join-required prompts in order/stock notification groups.
    if message.chat.type != "private":
        return

    telegram_id = message.from_user.id
    start_payload = (command.args or "").strip() if command else ""
    deep_link_product_id = None
    if start_payload.startswith("product_"):
        product_id_text = start_payload.removeprefix("product_")
        if product_id_text.isdigit():
            deep_link_product_id = int(product_id_text)

    # ═══════════════════════════════════════════════════════
    # CHANNEL MEMBERSHIP CHECK (skip for admins)
    # ═══════════════════════════════════════════════════════
    if telegram_id not in ADMIN_IDS:
        is_member = await check_user_membership(
            message.bot,
            telegram_id,
            fast=deep_link_product_id is not None,
        )
        if not is_member:
            await answer_card(message,
                "⚠️ <b>Access Restricted</b>\n\n"
                "You must join every required chat to use the bot.\n\n"
                "👇 Join below, then press <b>Try Again</b>",
                reply_markup=get_join_keyboard(),
                parse_mode="HTML"
            )
            return

    username = message.from_user.username
    full_name = message.from_user.full_name
    ref_payload = "" if deep_link_product_id is not None else start_payload
    is_admin = telegram_id in ADMIN_IDS

    db_task = asyncio.to_thread(
        _get_or_create_user, telegram_id, username, full_name, ref_payload
    )

    try:
        result = await db_task
    except Exception:
        logger.exception("Failed to create/update user %s on /start", telegram_id)
        await answer_card(message,
            "❌ <b>Startup Error</b>\n\nSomething went wrong starting your session.\nPlease try again with /start",
            parse_mode="HTML"
        )
        return

    user = SimpleNamespace(**result["profile"])
    if deep_link_product_id is not None:
        from handlers.products import product_info

        await product_info(message, deep_link_product_id)
        return

    bot_username = await _get_bot_username(message.bot)
    user.username = username
    user.full_name = full_name
    text = _build_start_welcome(user, full_name, telegram_id, username, bot_username)
    keyboard = get_admin_main_menu() if is_admin else get_main_menu()
    await answer_card(message,
        text, reply_markup=keyboard, parse_mode="HTML",
        disable_web_page_preview=True
    )
