# handlers/deposit.py

import re
import uuid
import secrets
import logging
import asyncio
import time
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy import desc

from states.deposit_states import DepositState
from database import SessionLocal
from models.deposit import Deposit
from models.promocode import PromoCode
from models.user import User
from services.deposit_checker import verify_deposit
from keyboards.deposit import get_deposit_menu
import config
from config import BEP20_ADDRESS, POLYGON_ADDRESS, BINANCE_PAY_ID
from utils.ui import show, update_card, answer_card

logger = logging.getLogger(__name__)

router = Router()

# ╔══════════════════════════════════════════════════════════════╗
# ║                     CONFIGURATION                           ║
# ╚══════════════════════════════════════════════════════════════╝

NETWORK_CONFIG = {
    "BEP20": {
        "icon": "🟡",
        "label": "BEP20 (BSC)",
        "description": "Binance Smart Chain",
        "style": "primary",
        "currency": "USDT",
        "symbol": "$",
    },
    "POLYGON": {
        "icon": "🟣",
        "label": "Polygon (MATIC)",
        "description": "Polygon Network",
        "style": "success",
        "currency": "USDT",
        "symbol": "$",
    },
    "BINANCE": {
        "icon": "🔴",
        "label": "Binance Pay",
        "description": "Pay with any crypto",
        "style": "danger",
        "currency": "USDT",
        "symbol": "$",
    },
    "UPI": {
        "icon": "🟢",
        "label": "UPI",
        "description": "Indian Rupee (₹)",
        "style": "primary",
        "currency": "INR",
        "symbol": "₹",
    },
    "PROMO": {
        "icon": "🏷️",
        "label": "Promocode",
        "description": "Promo Code Redemption",
        "style": "primary",
        "currency": "USDT",
        "symbol": "$",
    },
}

NETWORK_ADDRESSES = {
    "BEP20": BEP20_ADDRESS,
    "POLYGON": POLYGON_ADDRESS,
}

NETWORK_CALLBACKS = {
    "deposit_bep20": "BEP20",
    "deposit_polygon": "POLYGON",
    "deposit_binance": "BINANCE",
    "deposit_upi": "UPI",
}

CRYPTO_NETWORKS = {"BEP20", "POLYGON"}
BINANCE_PAY_NETWORK = "BINANCE"

TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
UTR_RE = re.compile(r"^\d{12}$")
TXN_ID_RE = re.compile(r"^(?:FMPIB)?\d{8,15}$", re.IGNORECASE)
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")
PROMO_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")

# Pagination settings for deposit history
DEPOSITS_PER_PAGE = 5


# ╔══════════════════════════════════════════════════════════════╗
# ║                   UI HELPERS                                ║
# ╚══════════════════════════════════════════════════════════════╝

def _divider(char: str = "━", length: int = 30) -> str:
    return char * length


def _border_box(title: str, emoji: str = "💰") -> str:
    return (
        f"╔{_divider('═', 28)}╗\n"
        f"║  {emoji} {title:<23}║\n"
        f"╚{_divider('═', 28)}╝"
    )


def _format_value(value, places: str) -> str:
    """Format a Decimal value without exposing accounting-only precision."""
    try:
        rounded = Decimal(str(value or 0)).quantize(Decimal(places), rounding=ROUND_HALF_UP)
        return format(rounded, "f").rstrip("0").rstrip(".") or "0"
    except Exception:
        return "0"


def _format_usdt(value) -> str:
    return f"${_format_value(value, '0.001')} USDT"


def _format_inr(value) -> str:
    return f"₹{_format_value(value, '0.01')} INR"


def _format_amount(amount, network: str) -> str:
    """Compatibility formatter for payment-start screens.

    Completed UPI history uses both INR and USDT separately; this helper is
    for the requested amount shown before a payment is verified.
    """
    if (network or "").upper() == "UPI":
        return _format_inr(amount)
    return _format_usdt(amount)


def _format_timestamp() -> str:
    return datetime.now().strftime("%d %b %Y, %I:%M:%S %p")


def _format_dt(dt_value) -> str:
    """Format a datetime object to readable string."""
    if dt_value is None:
        return "N/A"
    if isinstance(dt_value, datetime):
        return dt_value.strftime("%d %b %Y, %I:%M %p")
    try:
        return str(dt_value)[:19]
    except Exception:
        return "N/A"


def _get_network_info(network: str) -> dict:
    return NETWORK_CONFIG.get(network, {
        "icon": "💰",
        "label": network,
        "description": "",
        "style": "primary",
        "currency": "USDT",
        "symbol": "$",
    })


def _status_emoji(status: str) -> str:
    """Get emoji for deposit status."""
    status_map = {
        "completed": "✅",
        "pending": "⏳",
        "waiting_hash": "🔍",
        "failed": "❌",
        "expired": "⏰",
    }
    return status_map.get(status, "❓")


def _status_label(status: str) -> str:
    """Get human-readable label for deposit status."""
    status_map = {
        "completed": "Completed",
        "pending": "Pending",
        "waiting_hash": "Awaiting TXID",
        "failed": "Failed",
        "expired": "Expired",
    }
    return status_map.get(status, status.capitalize())


# ╔══════════════════════════════════════════════════════════════╗
# ║           AUTO-CLOSE SUCCESS MESSAGE HELPER                ║
# ╚══════════════════════════════════════════════════════════════╝

async def _auto_close_success_message(
    bot,
    chat_id: int,
    message_id: int,
    delay_seconds: int = 60,
):
    """
    After a delay, automatically edit the success/thank-you message
    to transition back to the main menu.
    This prevents the "thank you" message from staying open forever.
    """
    await asyncio.sleep(delay_seconds)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "🏠 <b>Main Menu</b>\n\n"
                "Welcome back! What would you like to do?\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>Your deposit was confirmed successfully.</i>\n\n"
                "Use the buttons below to navigate:"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🛍 Shop Now",
                            callback_data="products_menu",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="💰 Make a Deposit",
                            callback_data="deposit_start",
                        ),
                        InlineKeyboardButton(
                            text="📜 My Orders",
                            callback_data="orders_menu",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="🏠 Main Menu",
                            callback_data="main_menu",
                        ),
                    ],
                ]
            ),
        )
    except Exception:
        # Message might have been deleted, already edited by user action,
        # or the user clicked a button before the timer fired.
        pass


# ╔══════════════════════════════════════════════════════════════╗
# ║                STANDARD KEYBOARDS (NO SUPPORT BUTTONS)      ║
# ╚══════════════════════════════════════════════════════════════╝

def _cancel_kb() -> InlineKeyboardMarkup:
    """Cancel button - returns to deposit method selection."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel Deposit",
                    callback_data="deposit_start",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Main Menu",
                    callback_data="main_menu",
                )
            ],
        ]
    )


def _back_to_menu_kb() -> InlineKeyboardMarkup:
    """Back to main menu button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Main Menu",
                    callback_data="main_menu",
                )
            ]
        ]
    )


def _promo_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel button for promo code input."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="deposit_start",
                )
            ]
        ]
    )


def _promo_retry_kb() -> InlineKeyboardMarkup:
    """Retry or cancel for promo code."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Try Another Code",
                    callback_data="deposit_promo",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Main Menu",
                    callback_data="main_menu",
                ),
            ],
        ]
    )


def _status_kb(status: str, network: str) -> InlineKeyboardMarkup:
    """Create contextual keyboard based on deposit result status. NO support buttons."""
    buttons = []

    if status == "success":
        buttons.append([
            InlineKeyboardButton(
                text="🛍 Shop Now",
                callback_data="products_menu",
            ),
        ])
    elif status == "pending":
        buttons.append([
            InlineKeyboardButton(
                text="📜 View My Orders",
                callback_data="orders_menu",
            ),
        ])
    else:  # failure
        buttons.append([
            InlineKeyboardButton(
                text="🔄 Try Again",
                callback_data="deposit_start",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            text="🏠 Main Menu",
            callback_data="main_menu",
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ╔══════════════════════════════════════════════════════════════╗
# ║            MY DEPOSITS — VIEW DEPOSIT HISTORY              ║
# ╚══════════════════════════════════════════════════════════════╝

def _get_user_deposits(telegram_id: int, page: int = 0) -> tuple:
    """Get paginated deposit history for a user. Returns (deposits_list, total_count)."""
    db = SessionLocal()
    try:
        query = (
            db.query(Deposit)
            .filter(Deposit.telegram_id == telegram_id)
            .order_by(desc(Deposit.id))
        )
        total = query.count()
        deposits = (
            query
            .offset(page * DEPOSITS_PER_PAGE)
            .limit(DEPOSITS_PER_PAGE)
            .all()
        )
        return deposits, total
    finally:
        db.close()


def _build_deposit_card(deposit, index: int) -> str:
    """Build a formatted card for a single deposit."""
    network_info = _get_network_info(deposit.network or "unknown")
    icon = network_info["icon"]
    status_emoji = _status_emoji(deposit.status)
    status_label = _status_label(deposit.status)

    created_str = _format_dt(deposit.created_at if hasattr(deposit, 'created_at') else None)

    tx_display = "N/A"
    if deposit.tx_hash:
        if deposit.tx_hash.startswith("PROMO_"):
            tx_display = f"🎫 {deposit.tx_hash[6:]}"
        elif len(deposit.tx_hash) > 20:
            tx_display = f"{deposit.tx_hash[:12]}...{deposit.tx_hash[-6:]}"
        else:
            tx_display = deposit.tx_hash

    amount_lines = []
    if (deposit.network or "").upper() == "UPI" and deposit.status == "completed":
        # New records retain both values. Older UPI deposits only have the
        # converted USDT amount, so do not incorrectly label it as INR.
        if deposit.inr_amount is not None:
            amount_lines.append(f"├─ 🇮🇳 <b>Paid:</b> {_format_inr(deposit.inr_amount)}")
        amount_lines.append(f"├─ 💰 <b>Credited:</b> {_format_usdt(deposit.received_amount or deposit.amount)}")
        if deposit.conversion_rate is not None:
            amount_lines.append(f"├─ 💱 <b>Rate:</b> ₹{_format_value(deposit.conversion_rate, '0.001')} / USDT")
    elif (deposit.network or "").upper() == "UPI":
        amount_lines.append(f"├─ 🇮🇳 <b>Expected:</b> {_format_inr(deposit.amount)}")
    else:
        amount_lines.append(f"├─ 💰 <b>Amount:</b> {_format_usdt(deposit.received_amount or deposit.amount)}")

    card = (
        f"{status_emoji} <b>Deposit #{deposit.id}</b>\n"
        f"├─ {icon} <b>Network:</b> {network_info['label']}\n"
        + "\n".join(amount_lines) + "\n"
        + f"├─ 🔑 <b>Ref:</b> <code>{tx_display}</code>\n"
        + f"├─ 📅 <b>Date:</b> {created_str}\n"
        + f"└─ 📊 <b>Status:</b> {status_label}\n"
    )
    return card


@router.callback_query(F.data == "my_deposits")
async def my_deposits_handler(callback: CallbackQuery):
    """Show user's deposit history (page 0)."""
    await _show_deposits_page(callback, page=0)


@router.callback_query(F.data.startswith("deposits_page_"))
async def deposits_page_handler(callback: CallbackQuery):
    """Handle pagination for deposit history."""
    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0
    await _show_deposits_page(callback, page=page)


async def _show_deposits_page(callback: CallbackQuery, page: int = 0):
    """Build and show a page of deposit history."""
    await callback.answer()
    telegram_id = callback.from_user.id

    deposits, total = await asyncio.to_thread(_get_user_deposits, telegram_id, page)

    if total == 0:
        text = (
            f"📜 <b>MY DEPOSITS</b>\n\n"
            f"<b>No deposits found!</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"💡 You haven't made any deposits yet.\n"
            f"Start by selecting a payment method below.\n\n"
            f"{_divider('─', 28)}"
        )
        await show(
            callback,
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💰 Make a Deposit", callback_data="deposit_start")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
                ]
            ),
        )
        return

    total_pages = (total + DEPOSITS_PER_PAGE - 1) // DEPOSITS_PER_PAGE
    start_idx = page * DEPOSITS_PER_PAGE + 1
    end_idx = min(start_idx + DEPOSITS_PER_PAGE - 1, total)

    cards = []
    for deposit in deposits:
        cards.append(_build_deposit_card(deposit, 0))

    text = (
        f"📜 <b>MY DEPOSITS</b>\n\n"
        f"<b>Showing {start_idx}-{end_idx} of {total} deposits</b>\n"
        f"{_divider('─', 28)}\n\n"
        + "\n".join(cards) +
        f"\n{_divider('─', 28)}\n"
        f"<i>Page {page + 1} of {total_pages}</i>"
    )

    # Build pagination buttons
    nav_buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️ Previous", callback_data=f"deposits_page_{page - 1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"deposits_page_{page + 1}"))
    if row:
        nav_buttons.append(row)

    nav_buttons.append([InlineKeyboardButton(text="💰 Make a Deposit", callback_data="deposit_start")])
    nav_buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")])

    await show(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_buttons),
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              VALIDATION HELPERS                             ║
# ╚══════════════════════════════════════════════════════════════╝

def _valid_upi_reference(txid: str) -> bool:
    val = (txid or "").strip()
    return bool(
        UTR_RE.fullmatch(val)
        or TXN_ID_RE.fullmatch(val)
    )

def _valid_order_id(value: str) -> bool:
    return bool(ORDER_ID_RE.match(value))


def _valid_promo_code(value: str) -> bool:
    return bool(PROMO_CODE_RE.match(value))


# ╔══════════════════════════════════════════════════════════════╗
# ║              PROMOCODE HELPERS                              ║
# ╚══════════════════════════════════════════════════════════════╝

def _validate_and_get_promo(code: str):
    """Validate a promocode. Returns (is_valid, data_or_error)."""
    db = SessionLocal()
    try:
        promo = (
            db.query(PromoCode)
            .filter(PromoCode.code == code.upper())
            .first()
        )

        if not promo:
            return False, {"error": f"❌ Invalid promocode:\n<code>{code.upper()}</code>"}

        if not promo.is_active:
            return False, {"error": "❌ This promocode has been deactivated."}

        if not promo.can_use():
            return False, {"error": "❌ This promocode has reached its usage limit."}

        promo_amount = Decimal(str(promo.amount))
        return True, {"promo": promo, "promo_amount": promo_amount}

    finally:
        db.close()


def _check_promo_used(telegram_id: int, code: str) -> bool:
    """Check if user already redeemed this promocode."""
    db = SessionLocal()
    try:
        already_used = (
            db.query(Deposit)
            .filter(
                Deposit.telegram_id == telegram_id,
                Deposit.tx_hash == f"PROMO_{code.upper()}",
                Deposit.status == "completed",
            )
            .first()
        )
        return already_used is not None
    finally:
        db.close()


def _apply_promo_to_user(telegram_id: int, promo_code: str, promo_amount: Decimal):
    """Apply promocode: credit balance, increment usage, create deposit record."""
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.telegram_id == telegram_id)
            .first()
        )

        if not user:
            return False, "❌ User not found.\nPlease use /start first."

        promo = (
            db.query(PromoCode)
            .filter(PromoCode.code == promo_code.upper())
            .first()
        )

        if not promo:
            return False, "❌ Promocode no longer available."

        user.balance += promo_amount
        promo.used_count += 1

        deposit = Deposit(
            telegram_id=telegram_id,
            amount=float(promo_amount),
            tx_hash=f"PROMO_{promo_code.upper()}",
            network="PROMO",
            status="completed",
        )

        db.add(deposit)
        db.commit()
        db.refresh(user)

        logger.info(
            "Promo redeemed | Code=%s | Amount=%s | User=%s",
            promo_code.upper(),
            promo_amount,
            telegram_id,
        )

        return True, {
            "code": promo_code.upper(),
            "amount": promo_amount,
            "balance": user.balance,
        }

    except Exception as e:
        db.rollback()
        logger.exception("Promo redeem error")
        return False, "❌ An unexpected error occurred while applying promocode."

    finally:
        db.close()


# ╔══════════════════════════════════════════════════════════════╗
# ║              DEPOSIT MENU (ENTRY POINT)                     ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "deposit_start")
async def deposit_menu(callback: CallbackQuery, state: FSMContext):
    """Display the deposit method selection menu."""
    started = time.perf_counter()
    try:
        await callback.answer()
        active = await asyncio.to_thread(_active_invoice, callback.from_user.id)
        if active:
            await state.update_data(
                deposit_id=active["id"],
                invoice_code=active["code"],
                invoice_expires_at=active["expires_at"],
                amount_usdt=active["amount_usdt"],
                network=active["network"],
            )
        else:
            await state.clear()

        text = (
            f"💰 <b>DEPOSIT FUNDS</b>\n\n"
            f"<b>Choose a payment method.</b> You will create one 5-minute invoice and can switch methods without creating another invoice.\n\n"
            f"{_divider('─', 28)}\n\n"
            f"🟡 <b>USDT (BEP20)</b>\n"
            f"   └ Fast & low fees · Binance Smart Chain\n\n"
            f"🟣 <b>USDT (Polygon)</b>\n"
            f"   └ Cheap transactions · Polygon Network\n\n"
            f"🔴 <b>Binance Pay</b>\n"
            f"   └ Pay with any crypto · Instant\n\n"
            f"🟢 <b>UPI (₹ INR)</b>\n"
            f"   └ Indian Rupee · Bank transfer\n\n"
            f"🏷️ <b>Redeem Promocode</b>\n"
            f"   └ Use a promo code to get bonus credits\n\n"
            f"{_divider('─', 28)}\n\n"
            f"💡 <i>After paying, submit the transaction reference to verify it automatically.</i>"
        )

        await show(
            callback,
            text,
            parse_mode="HTML",
            reply_markup=get_deposit_menu(),
            state=state,
        )
    finally:
        logger.info(
            "button_timing button=deposit user_id=%s total_ms=%.1f",
            callback.from_user.id,
            (time.perf_counter() - started) * 1000,
        )


# ╔══════════════════════════════════════════════════════════════╗
# ║           DIRECT PROMOCODE REDEMPTION FROM MENU             ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "deposit_promo")
async def deposit_promo_start(callback: CallbackQuery, state: FSMContext):
    """Start direct promocode redemption from deposit menu."""
    await callback.answer()

    await show(
        callback,
        (
            f"🏷️ <b>REDEEM PROMOCODE</b>\n\n"
            f"<b>Enter your promocode to get bonus credits!</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"💡 <b>How it works:</b>\n"
            f"• Enter a valid promo code\n"
            f"• Get bonus USDT added instantly\n"
            f"• Each code can be used only once per user\n\n"
            f"<i>Send your promocode or type CANCEL</i>"
        ),
        parse_mode="HTML",
        reply_markup=_promo_cancel_kb(),
        state=state,
    )

    await state.set_state(DepositState.waiting_promo_code)


@router.message(DepositState.waiting_promo_code)
async def process_promo_code(message: Message, state: FSMContext):
    """Process promocode from direct redemption flow."""
    text = (message.text or "").strip().upper()

    if text == "CANCEL":
        await update_card(
            message, state,
            "❌ <b>Promocode Redemption Cancelled</b>",
            parse_mode="HTML",
            reply_markup=_back_to_menu_kb(),
        )
        await state.clear()
        return

    if not _valid_promo_code(text):
        await update_card(
            message, state,
            (
                f"⚠️ <b>Invalid Promocode Format</b>\n\n"
                f"{_divider('─', 28)}\n\n"
                f"❌ Promocodes should be 4-20 alphanumeric characters.\n\n"
                f"<i>Please check your code and try again, or type CANCEL.</i>"
            ),
            parse_mode="HTML",
            reply_markup=_promo_cancel_kb(),
        )
        return

    is_valid, promo_data = _validate_and_get_promo(text)

    if not is_valid:
        await update_card(
            message, state,
            promo_data["error"],
            parse_mode="HTML",
            reply_markup=_promo_retry_kb(),
        )
        await state.clear()
        return

    if _check_promo_used(message.from_user.id, text):
        await update_card(
            message, state,
            (
                f"⚠️ <b>Already Redeemed</b>\n\n"
                f"{_divider('─', 28)}\n\n"
                f"❌ You have already redeemed this promocode:\n"
                f"<code>{text}</code>\n\n"
                f"<i>Each promocode can only be used once per user.</i>"
            ),
            parse_mode="HTML",
            reply_markup=_promo_retry_kb(),
        )
        await state.clear()
        return

    success, result = _apply_promo_to_user(
        message.from_user.id, text, promo_data["promo_amount"]
    )

    if not success:
        await update_card(
            message, state,
            result,
            parse_mode="HTML",
            reply_markup=_back_to_menu_kb(),
        )
        await state.clear()
        return

    # Get card message IDs from state for auto-close timer
    data_before_clear = await state.get_data()
    card_chat_id = data_before_clear.get("_card_chat_id", message.chat.id)
    card_message_id = data_before_clear.get("_card_message_id")

    await state.clear()

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  GROUP NOTIFICATION — PROMOCODE REDEMPTION                 ║
    # ╚══════════════════════════════════════════════════════════════╝
    if getattr(config, 'GROUP_NOTIFICATIONS', False) and hasattr(config, 'GROUP_ID'):
        try:
            now = datetime.now().strftime("%d-%b-%Y %I:%M %p IST")
            uid = str(message.from_user.id)
            masked_uid = f"{uid[:4]}***{uid[-3:]}" if len(uid) >= 7 else uid

            group_msg = (
                "<code>$ journalctl --wallet</code>\n"
                "<code>New wallet event detected.</code>\n"
                "<code>━━━━━━━━━━━━━━━━━━━━━━</code>\n"
                f"<code>ACTION      PROMO</code>\n"
                f"<code>USER        {masked_uid}</code>\n"
                f"<code>CODE        {result['code']}</code>\n"
                f"<code>AMOUNT      ${result['amount']:,.2f}</code>\n"
                f"<code>STATUS      REDEEMED</code>\n"
                f"<code>TIME        {now}</code>\n"
                "<code>━━━━━━━━━━━━━━━━━━━━━━</code>\n"
                "<code>Wallet synchronized.</code>"
            )
            await message.bot.send_message(
                chat_id=config.GROUP_ID,
                text=group_msg,
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Failed to send group promo notification")

    # Show success message
    promo_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Shop Now", callback_data="products_menu")],
            [InlineKeyboardButton(text="💰 Make a Deposit", callback_data="deposit_start")],
            [InlineKeyboardButton(text="📜 My Deposits", callback_data="my_deposits")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
        ]
    )

    if card_chat_id and card_message_id:
        await update_card(
            message, None,
            (
                f"🎉 <b>PROMOCODE REDEEMED!</b>\n\n"
                f"<b>Congratulations!</b> Your promo code has been applied.\n\n"
                f"{_divider('═', 28)}\n\n"
                f"🏷️ <b>Code:</b> <code>{result['code']}</code>\n"
                f"💰 <b>Bonus:</b> +${result['amount']:,.2f} USDT\n"
                f"💳 <b>New Balance:</b> ${result['balance']:,.2f} USDT\n\n"
                f"{_divider('═', 28)}\n\n"
                f"✅ <b>Status:</b> Balance Credited Instantly\n"
                f"🛍 Your bonus is ready to use!\n\n"
                f"<i>Thank you for using the promocode! 🙏</i>"
            ),
            chat_id=card_chat_id,
            message_id=card_message_id,
            parse_mode="HTML",
            reply_markup=promo_kb,
        )
    else:
        await update_card(
            message, None,
            (
                f"🎉 <b>PROMOCODE REDEEMED!</b>\n\n"
                f"<b>Congratulations!</b> Your promo code has been applied.\n\n"
                f"{_divider('═', 28)}\n\n"
                f"🏷️ <b>Code:</b> <code>{result['code']}</code>\n"
                f"💰 <b>Bonus:</b> +${result['amount']:,.2f} USDT\n"
                f"💳 <b>New Balance:</b> ${result['balance']:,.2f} USDT\n\n"
                f"{_divider('═', 28)}\n\n"
                f"✅ <b>Status:</b> Balance Credited Instantly\n"
                f"🛍 Your bonus is ready to use!\n\n"
                f"<i>Thank you for using the promocode! 🙏</i>"
            ),
            parse_mode="HTML",
            reply_markup=promo_kb,
        )

    # ╔══════════════════════════════════════════════════════════════╗
    # ║  AUTO-CLOSE: After 60 sec, transition back to main menu    ║
    # ╚══════════════════════════════════════════════════════════════╝
    if card_chat_id and card_message_id:
        asyncio.create_task(
            _auto_close_success_message(
                bot=message.bot,
                chat_id=card_chat_id,
                message_id=card_message_id,
                delay_seconds=60,
            )
        )


# ╔══════════════════════════════════════════════════════════════╗
# ║              NETWORK SELECTION                              ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.in_(NETWORK_CALLBACKS.keys()))
async def select_network(callback: CallbackQuery, state: FSMContext):
    """Select or switch the payment method for a single active invoice."""
    await callback.answer()
    network = NETWORK_CALLBACKS[callback.data]
    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    expires_at = data.get("invoice_expires_at")
    if not deposit_id:
        active = await asyncio.to_thread(_active_invoice, callback.from_user.id)
        if active:
            deposit_id = active["id"]
            expires_at = active["expires_at"]
            data = {
                **data,
                "deposit_id": active["id"],
                "invoice_code": active["code"],
                "invoice_expires_at": active["expires_at"],
                "amount_usdt": active["amount_usdt"],
                "network": active["network"],
            }
    if deposit_id and expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
        except (TypeError, ValueError):
            expiry = datetime.min
        if expiry > datetime.utcnow():
            if not await asyncio.to_thread(_switch_invoice_network, int(deposit_id), network):
                await callback.message.answer("This invoice is no longer switchable. Start a new deposit.")
                await state.clear()
                return
            await state.update_data(
                deposit_id=int(deposit_id),
                invoice_code=data.get("invoice_code"),
                invoice_expires_at=expires_at,
                amount_usdt=data.get("amount_usdt"),
                network=network,
            )
            await state.set_state(DepositState.waiting_txid)
            await _show_invoice(callback, state, int(deposit_id), network, data)
            return
        await state.clear()

    try:
        invoice = await asyncio.to_thread(_create_invoice, callback.from_user.id, Decimal("0"), network)
    except Exception:
        logger.exception("Failed to create payment invoice")
        await show(callback, "❌ Could not create an invoice. Please try again.", reply_markup=_back_to_menu_kb())
        return
    invoice_data = {
        **data,
        "deposit_id": invoice["id"],
        "invoice_code": invoice["code"],
        "invoice_expires_at": invoice["expires_at"],
        "amount_usdt": invoice["amount_usdt"],
        "network": network,
    }
    await state.update_data(**invoice_data)
    await state.set_state(DepositState.waiting_txid)
    await _show_invoice(callback, state, int(invoice["id"]), network, invoice_data)


# ╔══════════════════════════════════════════════════════════════╗
# ║              AMOUNT PROCESSING                              ║
# ╚══════════════════════════════════════════════════════════════╝

def _create_invoice(telegram_id: int, amount_usdt: Decimal, network: str) -> dict:
    """Create one five-minute invoice shared by all payment methods."""
    db = SessionLocal()
    try:
        active = (db.query(Deposit)
                  .filter(Deposit.telegram_id == telegram_id,
                          Deposit.status.in_(["waiting_hash", "pending"]),
                          Deposit.tx_hash.is_(None),
                          Deposit.invoice_expires_at > datetime.utcnow())
                  .order_by(Deposit.id.desc()).first())
        if active:
            active.network = network
            active.invoice_usdt_amount = amount_usdt
            active.amount = Decimal("0") if network in {"UPI", "BINANCE"} else amount_usdt
            db.commit()
            return {"id": active.id, "code": active.invoice_code or active.order_id,
                    "expires_at": active.invoice_expires_at.isoformat(),
                    "amount_usdt": str(active.invoice_usdt_amount)}
        code = None
        for _ in range(8):
            candidate = f"PAYOUT{secrets.randbelow(900000) + 100000}"
            if not db.query(Deposit.id).filter(Deposit.invoice_code == candidate).first():
                code = candidate
                break
        if not code:
            raise RuntimeError("Could not allocate a unique invoice code")
        expires = datetime.utcnow() + timedelta(minutes=5)
        deposit = Deposit(
            telegram_id=telegram_id,
            order_id=code,
            invoice_code=code,
            invoice_expires_at=expires,
            invoice_usdt_amount=amount_usdt,
            amount=Decimal("0") if network in {"UPI", "BINANCE"} else amount_usdt,
            network=network,
            status="waiting_hash",
        )
        db.add(deposit)
        db.commit()
        db.refresh(deposit)
        return {"id": deposit.id, "code": code, "expires_at": expires.isoformat(), "amount_usdt": str(amount_usdt)}
    finally:
        db.close()


def _active_invoice(telegram_id: int) -> dict | None:
    db = SessionLocal()
    try:
        deposit = (db.query(Deposit)
                   .filter(Deposit.telegram_id == telegram_id,
                           Deposit.status.in_(["waiting_hash", "pending"]),
                           Deposit.tx_hash.is_(None),
                           Deposit.invoice_expires_at > datetime.utcnow())
                   .order_by(Deposit.id.desc()).first())
        if not deposit:
            return None
        return {"id": deposit.id, "code": deposit.invoice_code or deposit.order_id,
                "expires_at": deposit.invoice_expires_at.isoformat(),
                "amount_usdt": str(deposit.invoice_usdt_amount or 0),
                "network": deposit.network}
    finally:
        db.close()


def _switch_invoice_network(deposit_id: int, network: str) -> bool:
    db = SessionLocal()
    try:
        deposit = db.query(Deposit).filter(Deposit.id == deposit_id).first()
        if not deposit or deposit.status not in {"waiting_hash", "pending"} or deposit.tx_hash:
            return False
        if deposit.invoice_expires_at and deposit.invoice_expires_at <= datetime.utcnow():
            return False
        deposit.network = network
        deposit.amount = Decimal("0") if network in {"UPI", "BINANCE"} else Decimal(str(deposit.invoice_usdt_amount or 0))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _invoice_keyboard() -> InlineKeyboardMarkup:
    # Keep the controls from the original deposit card.
    return _cancel_kb()


def _invoice_payload(network: str, data: dict) -> tuple[str, str | None]:
    """Build the original deposit-card copy for the combined QR/photo card."""
    code = escape(str(data.get("invoice_code", "")))
    deposit_id = escape(str(data.get("deposit_id", "")))
    expiry = datetime.fromisoformat(data["invoice_expires_at"])
    remaining = max(0, int((expiry - datetime.utcnow()).total_seconds()))
    mins, secs = divmod(remaining, 60)
    network_config = _get_network_info(network)
    divider = _divider("─", 28)
    qr = None

    if network == "UPI":
        upi_id = escape(str(getattr(config, "UPI_ID", "") or "Not configured"))
        qr = getattr(config, "UPI_QR_FILE", "")
        payment_info = (
            f"🟢 <b>UPI Payment Details</b>\n\n"
            f"<blockquote><code>{upi_id}</code></blockquote>\n\n"
            "📱 Scan the QR above or use the UPI ID.\n"
            "💳 Pay any amount; the verified amount is credited.\n\n"
            "<b>📋 After payment, send:</b> UTR / Transaction ID"
        )
        reference = "UTR / Transaction reference"
    elif network in CRYPTO_NETWORKS:
        address = escape(str(NETWORK_ADDRESSES.get(network, "") or "Not configured"))
        qr = getattr(config, f"{network}_QR_FILE", "")
        payment_info = (
            f"{network_config['icon']} <b>{network_config['label']} Deposit Address</b>\n\n"
            f"<blockquote><code>{address}</code></blockquote>\n\n"
            "⚠️ <b>Important:</b> Send only USDT on the network shown above.\n"
            "💰 Send the amount you choose; the verified amount is credited.\n\n"
            "<b>📋 After sending, paste:</b> Transaction Hash (TXID)"
        )
        reference = "Transaction Hash (TXID)"
    else:
        pay_id = escape(str(BINANCE_PAY_ID or "Not configured"))
        payment_info = (
            "🔴 <b>Binance Pay Details</b>\n\n"
            f"<b>Pay ID:</b> <blockquote><code>{pay_id}</code></blockquote>\n\n"
            "📱 Open Binance Pay and send any amount.\n"
            "💰 The verified transfer amount is credited.\n\n"
            "<b>📋 After payment, send:</b> Binance Pay Order ID"
        )
        reference = "Binance Pay Order ID"

    body = (
        f"{network_config['icon']} <b>DEPOSIT CREATED</b>\n\n"
        f"<b>📋 Deposit Details</b>\n"
        f"{divider}\n\n"
        f"🆔 <b>Deposit ID:</b> <code>#{deposit_id}</code>\n"
        f"🧾 <b>Invoice:</b> <code>{code}</code>\n"
        f"📡 <b>Network:</b> {network_config['icon']} {network_config['label']}\n"
        f"⏳ <b>Expires in:</b> {mins}:{secs:02d}\n"
        f"📊 <b>Status:</b> ⏳ Awaiting Payment\n\n"
        f"{_divider('═', 28)}\n\n"
        f"{payment_info}\n\n"
        f"{_divider('═', 28)}\n\n"
        f"💡 After payment, send your <b>{reference}</b> here for verification."
    )
    return body, qr


async def _show_invoice(target, state: FSMContext, deposit_id: int, network: str, data: dict):
    body, qr = _invoice_payload(network, data)
    bot = target.bot
    chat_id = target.message.chat.id if isinstance(target, CallbackQuery) else target.chat.id
    previous_photo = data.get("invoice_photo_message_id")
    previous_card = data.get("_card_message_id")
    previous_chat = data.get("_card_chat_id") or chat_id

    # Remove the previous invoice view before recreating it, so a switched
    # payment method always appears as QR first and invoice/buttons below.
    if previous_card:
        try:
            await bot.delete_message(chat_id=int(previous_chat), message_id=int(previous_card))
        except Exception:
            pass
    if previous_photo:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(previous_photo))
        except Exception:
            pass

    invoice_message = None
    if qr:
        try:
            path = Path(qr)
            photo = FSInputFile(path) if path.is_file() else qr
            invoice_message = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=body,
                parse_mode="HTML",
                reply_markup=_invoice_keyboard(),
            )
        except Exception:
            logger.exception("Could not send configured %s QR image", network)

    # A configured QR image and its invoice details are a single Telegram
    # photo message (image with caption), matching the previous payment card.
    # Methods without a QR (for example Binance Pay) use one text card.
    if invoice_message is None:
        if isinstance(target, CallbackQuery):
            invoice_message = await target.message.answer(
                body, parse_mode="HTML", reply_markup=_invoice_keyboard()
            )
        else:
            invoice_message = await target.answer(
                body, parse_mode="HTML", reply_markup=_invoice_keyboard()
            )
    await state.update_data(
        _card_chat_id=invoice_message.chat.id,
        _card_message_id=invoice_message.message_id,
        invoice_card_message_id=invoice_message.message_id,
        invoice_photo_message_id=invoice_message.message_id if qr and invoice_message.photo else None,
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║           TXID / UTR / ORDER ID PROCESSING                  ║
# ╚══════════════════════════════════════════════════════════════╝

def _check_duplicate(txid: str) -> bool:
    db = SessionLocal()
    try:
        return db.query(Deposit).filter(Deposit.tx_hash == txid).first() is not None
    finally:
        db.close()


def _save_txid(deposit_id: int, txid: str) -> str:
    db = SessionLocal()
    try:
        deposit = db.query(Deposit).filter(Deposit.id == deposit_id).first()
        if not deposit:
            return "not_found"
        if deposit.invoice_expires_at and deposit.invoice_expires_at <= datetime.utcnow():
            deposit.status = "expired"
            db.commit()
            return "expired"
        deposit.tx_hash = txid
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return "duplicate"
        return "ok"
    finally:
        db.close()


def _set_pending_if_waiting(deposit_id: int) -> None:
    db = SessionLocal()
    try:
        dep = db.query(Deposit).filter(Deposit.id == deposit_id).first()
        if dep and dep.status == "waiting_hash":
            dep.status = "pending"
            db.commit()
    finally:
        db.close()


@router.message(DepositState.waiting_txid)
async def process_txid(message: Message, state: FSMContext):
    """Validate and process the transaction ID/UTR/order ID."""
    txid = message.text.strip() if message.text else ""

    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    network = data.get("network")
    card_chat_id = data.get("_card_chat_id")
    card_message_id = data.get("_card_message_id")
    network_config = _get_network_info(network)

    if not deposit_id:
        await state.clear()
        await update_card(
            message, None,
            "⚠️ <b>No Active Deposit</b>\n\nPlease start a new deposit.",
            chat_id=card_chat_id,
            message_id=card_message_id,
            parse_mode="HTML",
            reply_markup=_back_to_menu_kb(),
        )
        return

    # Format validation
    if network == "UPI":
        if not _valid_upi_reference(txid):
            await update_card(
                message, state,
                (
                    f"⚠️ <b>Invalid UTR/Transaction ID</b>\n\n"
                    f"{_divider('─', 28)}\n\n"
                    f"❌ Expected: 12-digit UTR or app reference\n"
                    f"   (e.g. 123456789012 or FMPIB6269486679)\n\n"
                    f"<i>Check your payment app and try again.</i>"
                ),
                parse_mode="HTML",
                reply_markup=_cancel_kb(),
            )
            return
    elif network == BINANCE_PAY_NETWORK:
        if not _valid_order_id(txid):
            await update_card(
                message, state,
                (
                    f"⚠️ <b>Invalid Order ID</b>\n\n"
                    f"{_divider('─', 28)}\n\n"
                    f"❌ Expected: Alphanumeric, 8-32 characters\n\n"
                    f"<i>Check your Binance Pay receipt and try again.</i>"
                ),
                parse_mode="HTML",
                reply_markup=_cancel_kb(),
            )
            return
    else:
        if not TX_HASH_RE.match(txid):
            await update_card(
                message, state,
                (
                    f"⚠️ <b>Invalid Transaction Hash</b>\n\n"
                    f"{_divider('─', 28)}\n\n"
                    f"❌ Expected: 0x + 64 hex characters (66 total)\n\n"
                    f"<i>Copy the full TXID from your wallet/explorer.</i>"
                ),
                parse_mode="HTML",
                reply_markup=_cancel_kb(),
            )
            return

    if await asyncio.to_thread(_check_duplicate, txid):
        await update_card(
            message, state,
            (
                f"⚠️ <b>Duplicate Detected</b>\n\n"
                f"{_divider('─', 24)}\n\n"
                f"❌ This reference has already been used.\n\n"
                f"If you believe this is an error, contact support."
            ),
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    result = await asyncio.to_thread(_save_txid, deposit_id, txid)

    if result == "not_found":
        await update_card(
            message, state,
            "⚠️ <b>Deposit Not Found</b>\n\nPlease start a new deposit.",
            parse_mode="HTML",
            reply_markup=_back_to_menu_kb(),
        )
        return
    if result == "expired":
        await state.clear()
        await update_card(message, None,
            "⏰ <b>Invoice expired</b>\n\nThis five-minute invoice has expired. Start a new deposit to get a fresh invoice.",
            chat_id=card_chat_id, message_id=card_message_id,
            parse_mode="HTML", reply_markup=_status_kb("failure", network))
        return
    if result == "duplicate":
        await update_card(
            message, state,
            "⚠️ <b>Duplicate Detected</b>\n\n❌ This reference is already in use.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    # Verification loading
    await update_card(
        message, state,
        (
            f"🔍 <b>Verifying Your Payment...</b>\n\n"
            f"{_divider('─', 24)}\n\n"
            f"🆔 Deposit: <code>#{deposit_id}</code>\n"
            f"📡 Network: {network_config['icon']} {network_config['label']}\n"
            f"🔑 Reference: <code>{txid[:20]}...</code>\n\n"
            f"⏳ <i>Checking with the network...</i>\n"
            f"<i>This may take a moment.</i>"
        ),
        parse_mode="HTML"
    )

    result_info: dict = {}
    success = await verify_deposit(deposit_id, result_info=result_info)
    await state.clear()

    # ═══════════════════════════════════════════════════════════════
    # SUCCESS
    # ═══════════════════════════════════════════════════════════════
    if success:

        # ╔══════════════════════════════════════════════════════════╗
        # ║  GROUP NOTIFICATION — TOP-UP (DEPOSIT)                 ║
        # ╚══════════════════════════════════════════════════════════╝
        if getattr(config, 'GROUP_NOTIFICATIONS', False) and hasattr(config, 'GROUP_ID'):
            try:
                now = datetime.now().strftime("%d-%b-%Y %I:%M %p IST")
                uid = str(message.from_user.id)
                masked_uid = f"{uid[:4]}***{uid[-3:]}" if len(uid) >= 7 else uid

                network_map = {
                    "BEP20": "BSC",
                    "POLYGON": "MATIC",
                    "BINANCE": "BINANCE",
                    "UPI": "UPI",
                }
                network_short = network_map.get(network, network.upper())

                group_msg = (
                    "<code>$ journalctl --wallet</code>\n"
                    "<code>New wallet event detected.</code>\n"
                    "<code>━━━━━━━━━━━━━━━━━━━━━━</code>\n"
                    f"<code>ACTION      TOP-UP</code>\n"
                    f"<code>USER        {masked_uid}</code>\n"
                    f"<code>NETWORK     {network_short}</code>\n"
                    f"<code>STATUS      CONFIRMED</code>\n"
                    f"<code>TIME        {now}</code>\n"
                    "<code>━━━━━━━━━━━━━━━━━━━━━━</code>\n"
                    "<code>Wallet synchronized.</code>"
                )
                await message.bot.send_message(
                    chat_id=config.GROUP_ID,
                    text=group_msg,
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("Failed to send group deposit notification")

        await update_card(
            message, None,
            (
                f"✅ <b>DEPOSIT CONFIRMED</b>\n\n"
                f"🎉 <b>Your deposit has been confirmed!</b>\n\n"
                f"{_divider('─', 28)}\n\n"
                f"🆔 <b>Deposit:</b> <code>#{deposit_id}</code>\n"
                f"📡 <b>Network:</b> {network_config['icon']} {network_config['label']}\n"
                f"🔑 <b>Ref:</b> <code>{txid[:24]}...</code>\n"
                f"🕐 <b>Time:</b> {_format_timestamp()}\n\n"
                f"{_divider('═', 28)}\n\n"
                f"✅ <b>Status:</b> Balance Credited\n"
                f"💰 Funds added to your account.\n\n"
                f"<i>Thank you for your deposit! 🙏</i>"
            ),
            chat_id=card_chat_id,
            message_id=card_message_id,
            parse_mode="HTML",
            reply_markup=_status_kb("success", network),
        )

        # ╔══════════════════════════════════════════════════════════════╗
        # ║  AUTO-CLOSE: After 60 sec, transition back to main menu    ║
        # ╚══════════════════════════════════════════════════════════════╝
        if card_chat_id and card_message_id:
            asyncio.create_task(
                _auto_close_success_message(
                    bot=message.bot,
                    chat_id=card_chat_id,
                    message_id=card_message_id,
                    delay_seconds=60,
                )
            )

        return

    # ═══════════════════════════════════════════════════════════════
    # FAILURE
    # ═══════════════════════════════════════════════════════════════
    if success is False:
        reason = result_info.get("reason")
        if reason == "attempt_limit":
            await update_card(
                message, None,
                (
                    f"⚠️ <b>VERIFICATION CANCELLED</b>\n\n"
                    f"We could not verify this payment after {result_info.get('attempts', 10)} checks. "
                    f"Automatic checking has stopped.\n\n"
                    f"🆔 Deposit: <code>#{deposit_id}</code>\n"
                    f"🔑 Ref: <code>{txid[:24]}...</code>\n\n"
                    f"The admin has been notified. If you paid successfully, contact support and include these details."
                ),
                chat_id=card_chat_id,
                message_id=card_message_id,
                parse_mode="HTML",
                reply_markup=_status_kb("failure", network),
            )
        elif reason == "underpaid":
            await update_card(
                message, None,
                (
                    f"⚠️ <b>AMOUNT MISMATCH</b>\n\n"
                    f"<b>You sent less than the required amount.</b>\n\n"
                    f"{_divider('─', 28)}\n\n"
                    f"📋 <b>Required:</b> {result_info.get('requested')}\n"
                    f"📥 <b>Received:</b> {result_info.get('received')}\n"
                    f"❌ <b>Shortfall:</b> {result_info.get('shortfall')}\n\n"
                    f"💡 Start a new deposit for the remaining amount.\n\n"
                    f"🆔 Deposit Ref: <code>#{deposit_id}</code>"
                ),
                chat_id=card_chat_id,
                message_id=card_message_id,
                parse_mode="HTML",
                reply_markup=_status_kb("failure", network),
            )
        elif reason == "duplicate":
            await update_card(
                message, None,
                (
                    f"⚠️ <b>Duplicate Payment</b>\n\n"
                    f"{_divider('─', 24)}\n\n"
                    f"❌ This reference has already been used.\n\n"
                    f"Contact support if you believe this is an error."
                ),
                chat_id=card_chat_id,
                message_id=card_message_id,
                parse_mode="HTML",
                reply_markup=_status_kb("failure", network),
            )
        else:
            await update_card(
                message, None,
                (
                    f"❌ <b>VERIFICATION FAILED</b>\n\n"
                    f"<b>We couldn't verify your payment.</b>\n\n"
                    f"{_divider('─', 28)}\n\n"
                    f"🆔 Deposit: <code>#{deposit_id}</code>\n"
                    f"🔑 Ref: <code>{txid[:24]}...</code>\n\n"
                    f"Possible reasons:\n"
                    f"• Transaction not found on network\n"
                    f"• Wrong network/address used\n"
                    f"• Transaction still pending (retry later)\n\n"
                    f"💡 Contact support with your deposit ID."
                ),
                chat_id=card_chat_id,
                message_id=card_message_id,
                parse_mode="HTML",
                reply_markup=_status_kb("failure", network),
            )
        return

    # ═══════════════════════════════════════════════════════════════
    # PENDING
    # ═══════════════════════════════════════════════════════════════
    await asyncio.to_thread(_set_pending_if_waiting, deposit_id)

    if network == "UPI":
        pending_msg = (
            f"⏳ <b>VERIFICATION PENDING</b>\n\n"
            f"<b>Your payment is being verified.</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"🏦 <b>Method:</b> UPI Bank Transfer\n"
            f"💳 <b>Reference:</b> <code>{txid}</code>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"⏱ <b>What happens next:</b>\n"
            f"• We check for your payment regularly\n"
            f"• Usually confirmed within 1-5 minutes\n"
            f"• You'll be notified automatically\n\n"
            f"💡 No need to resend your UTR."
        )
    elif network == BINANCE_PAY_NETWORK:
        pending_msg = (
            f"⏳ <b>VERIFICATION PENDING</b>\n\n"
            f"<b>Your Binance Pay order is being verified.</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"🔴 <b>Method:</b> Binance Pay\n"
            f"🧾 <b>Order ID:</b> <code>{txid}</code>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"⏱ <b>What happens next:</b>\n"
            f"• Binance confirms the order\n"
            f"• Usually within 1-5 minutes\n"
            f"• You'll be notified automatically"
        )
    else:
        pending_msg = (
            f"⏳ <b>VERIFICATION PENDING</b>\n\n"
            f"<b>Your transaction is being verified.</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"📡 <b>Network:</b> {network_config['icon']} {network_config['label']}\n"
            f"🔑 <b>TXID:</b> <code>{txid[:24]}...</code>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"⏱ <b>What happens next:</b>\n"
            f"• Network confirmations in progress\n"
            f"• Usually takes 2-10 minutes\n"
            f"• You'll be notified automatically\n\n"
            f"💡 No need to resend your TXID."
        )

    await update_card(
        message, None,
        pending_msg,
        chat_id=card_chat_id,
        message_id=card_message_id,
        parse_mode="HTML",
        reply_markup=_status_kb("pending", network),
    )
