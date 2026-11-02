
from utils.ui import answer_card, schedule_delete_card, schedule_success_notice
# handlers/admin.py — FIXED ADMIN PANEL WITH DASHBOARD, BROADCAST & FULL PROVIDER MANAGEMENT
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape as html_escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import func, or_, select

from database import SessionLocal
from config import ADMIN_IDS, MAINTENANCE_PASSWORD_HASH, MANAGER_WEB_URL

from models.user import User
from models.product import Product
from models.order import Order
from models.deposit import Deposit
from models.ticket import Ticket
from models.provider import Provider
from models.custom_rate import CustomRate
from models.rate_control import ProductRateControl, RateControlAssignment
from models.manager_auth import ManagerLoginChallenge
from models.admin_audit_log import AdminAuditLog

from keyboards.admin_menu import get_admin_panel
from keyboards.menu import get_admin_main_menu

from states.broadcast import BroadcastState
from services.maintenance_mode import is_maintenance_enabled, set_maintenance_enabled
from handlers.start import _build_start_welcome, _get_bot_username
from handlers.products import invalidate_catalog_custom_prices_cache

logger = logging.getLogger(__name__)
router = Router()


def _create_manager_web_link(
    telegram_id: int,
    telegram_chat_id: int,
    telegram_message_id: int,
) -> str:
    """Create a short-lived, single-use manager login token for this admin."""
    token = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.expires_at < now,
            ManagerLoginChallenge.consumed_at.is_(None),
        ).delete(synchronize_session=False)
        db.add(
            ManagerLoginChallenge(
                id=str(uuid.uuid4()),
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                created_at=now,
                expires_at=now + timedelta(minutes=2),
                approved_at=now,
                approved_by=telegram_id,
                consumed_at=None,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                card_cleaned_at=None,
            )
        )
        db.commit()
        return token
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _update_manager_web_link_card(token: str, telegram_chat_id: int, telegram_message_id: int) -> None:
    """Attach the final card id (including the edit fallback case) to a challenge."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db = SessionLocal()
    try:
        db.query(ManagerLoginChallenge).filter(
            ManagerLoginChallenge.token_hash == digest,
            ManagerLoginChallenge.consumed_at.is_(None),
        ).update(
            {
                ManagerLoginChallenge.telegram_chat_id: telegram_chat_id,
                ManagerLoginChallenge.telegram_message_id: telegram_message_id,
            },
            synchronize_session=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class AdminUserBalanceState(StatesGroup):
    waiting_amount = State()


class CustomRateState(StatesGroup):
    waiting_user_id = State()
    waiting_price = State()
    waiting_rate_control_price = State()


class ProviderSetupState(StatesGroup):
    waiting_name = State()
    waiting_key = State()
    waiting_base_url = State()
    waiting_api_type = State()
    waiting_auth_type = State()
    waiting_api_key = State()
    waiting_api_secret = State()
    waiting_configuration = State()


class ProviderEditState(StatesGroup):
    waiting_field_value = State()


class MaintenanceModeState(StatesGroup):
    waiting_password = State()


class ManagerWebState(StatesGroup):
    waiting_password = State()



ACTIVITY_PER_PAGE = 8


def _money(value) -> str:
    """Convert Decimal only when rendering Telegram text."""
    return f"${float(value or Decimal('0')):.2f}"


def safe(text: str) -> str:
    return html_escape(str(text), quote=False)


def _validate_base_url(url: str) -> bool:
    """Basic validation for base URLs."""
    u = url.strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def _custom_rate_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Set Custom Rate", callback_data="custom_rate_add")],
        [InlineKeyboardButton(text="🎛 Rate Control", callback_data="rate_control_menu")],
        [InlineKeyboardButton(text="📋 Active Custom Rates", callback_data="custom_rate_list")],
        [InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")],
    ])


async def _safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Safely edit message text ignoring Telegram 'message is not modified' errors."""
    try:
        edited = await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        schedule_success_notice(edited, text)
        return edited
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return None
        raise


# ╔══════════════════════════════════════════════════════════════╗
# ║  ADMIN PANEL KEYBOARD HELPER                                 ║
# ╚══════════════════════════════════════════════════════════════╝

def _get_admin_panel_with_providers() -> InlineKeyboardMarkup:
    """Dynamically appends the 🏪 Providers button to the admin panel keyboard."""
    base_markup = get_admin_panel()
    rows = [list(row) for row in base_markup.inline_keyboard]

    provider_btn = InlineKeyboardButton(text="🏪 Providers", callback_data="admin_providers")

    # Prevent duplicate button if already present
    has_provider_btn = any(
        getattr(btn, "callback_data", "") == "admin_providers"
        for row in rows
        for btn in row
    )

    if not has_provider_btn:
        if len(rows) > 1:
            rows.insert(-1, [provider_btn])
        else:
            rows.append([provider_btn])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "maintenance_mode")
async def maintenance_mode_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    enabled = await asyncio.to_thread(is_maintenance_enabled)
    status = "ON — user actions are blocked" if enabled else "OFF — the store is available"
    await state.set_state(MaintenanceModeState.waiting_password)
    await answer_card(callback,
        f"🛠 <b>Maintenance Mode</b>\nStatus: <b>{status}</b>\n\n"
        "Enter the maintenance password to continue.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Cancel", callback_data="maintenance_cancel")
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data == "maintenance_cancel")
async def maintenance_mode_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await answer_card(callback, "Maintenance mode change cancelled.")
    await callback.answer()


@router.message(MaintenanceModeState.waiting_password)
async def maintenance_mode_password(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    password = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    submitted_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(submitted_hash, MAINTENANCE_PASSWORD_HASH):
        await state.clear()
        await answer_card(message, "❌ Incorrect password. No changes were made.")
        return

    await state.clear()
    enabled = await asyncio.to_thread(is_maintenance_enabled)
    target_enabled = not enabled
    action = "enable" if target_enabled else "disable"
    label = "Turn ON" if target_enabled else "Turn OFF"
    await answer_card(message,
        f"Password verified. Confirm to <b>{action}</b> maintenance mode?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🛠 {label}", callback_data=f"maintenance_confirm_{int(target_enabled)}"),
            InlineKeyboardButton(text="Cancel", callback_data="maintenance_cancel"),
        ]]),
    )


@router.callback_query(F.data.in_({"maintenance_confirm_0", "maintenance_confirm_1"}))
async def maintenance_mode_confirm(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    enabled = callback.data.endswith("_1")
    try:
        await asyncio.to_thread(set_maintenance_enabled, enabled)
    except Exception:
        logger.exception("Failed to change maintenance mode")
        await callback.answer("Could not save the setting. Please try again.", show_alert=True)
        return
    status = "enabled. Users will only see the maintenance notice." if enabled else "disabled. The store is available again."
    updated = await callback.message.edit_text(f"✅ Maintenance mode {status}")
    schedule_success_notice(updated, f"✅ Maintenance mode {status}", owner_id=callback.from_user.id)
    await callback.answer("Maintenance setting saved.")


# ╔══════════════════════════════════════════════════════════════╗
# ║  ADMIN PANEL DASHBOARD — TERMINAL STYLE                     ║
# ╚══════════════════════════════════════════════════════════════╝

def _build_admin_dashboard(db, admin_name: str, admin_id: int) -> str:
    """Build the compact admin welcome screen with one database round trip."""
    counts = db.execute(select(
        select(func.count()).select_from(User).scalar_subquery(),
        select(func.count()).select_from(Product).scalar_subquery(),
        select(func.count()).select_from(Order).scalar_subquery(),
    )).one()
    users, products, orders = (int(value or 0) for value in counts)
    safe_admin_name = html_escape(admin_name or "Admin", quote=False)
    return "\n".join((
        "<blockquote>👑 <b>Rain Deals Admin Panel</b></blockquote>",
        f"Welcome, <b>{safe_admin_name}</b>!",
        "Manage your store, products, orders, and customers from here.",
        "",
        f"📦 Products: <b>{products}</b>  ·  🛍 Orders: <b>{orders}</b>  ·  👥 Users: <b>{users}</b>",
        "",
        "👇 <b>Choose an option below to get started.</b>",
    ))

def _load_admin_dashboard(admin_name: str, admin_id: int) -> str:
    """Run the synchronous dashboard queries outside aiogram's event loop."""
    db = SessionLocal()
    try:
        return _build_admin_dashboard(db, admin_name, admin_id)
    finally:
        db.close()


def _load_admin_stats() -> tuple[int, int, int, int, int, object]:
    """Fetch dashboard statistics with one DB round trip, off the event loop."""
    db = SessionLocal()
    try:
        row = db.execute(select(
            select(func.count()).select_from(User).scalar_subquery(),
            select(func.count()).select_from(Product).scalar_subquery(),
            select(func.count()).select_from(Order).scalar_subquery(),
            select(func.count()).select_from(Deposit).scalar_subquery(),
            select(func.count()).select_from(Ticket).scalar_subquery(),
            select(func.coalesce(func.sum(Order.amount), 0)).scalar_subquery(),
        )).one()
        return tuple(row)
    finally:
        db.close()

def _load_admin_product_rows() -> list[tuple[int, bool, str, str, int]]:
    """Fetch only the five columns rendered in the product list."""
    db = SessionLocal()
    try:
        rows = db.query(
            Product.id, Product.is_active, Product.icon, Product.name, Product.stock
        ).order_by(Product.id.desc()).all()
        return [
            (int(product_id), bool(active), icon or "📦", name, int(stock or 0))
            for product_id, active, icon, name, stock in rows
        ]
    finally:
        db.close()

def _load_user_dashboard_text(telegram_id: int) -> str | None:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        return _build_user_dashboard(user) if user else None
    finally:
        db.close()


# ╔══════════════════════════════════════════════════════════════╗
# ║  USER DASHBOARD — TERMINAL STYLE (with exact balance format) ║
# ╚══════════════════════════════════════════════════════════════╝

def _format_balance(raw_balance) -> str:
    """Display wallet values with at most three decimal places."""
    try:
        dec_val = Decimal(str(raw_balance or 0))
        rounded = dec_val.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        return format(rounded, "f").rstrip("0").rstrip(".") or "0"
    except Exception:
        return "0"


def _build_user_dashboard(user) -> str:
    """Build terminal-style user dashboard with exact fractional balance display."""
    balance_raw = getattr(user, 'balance_display', getattr(user, 'balance', 0))
    balance_str = _format_balance(balance_raw)

    total_orders = int(getattr(user, 'total_orders', 0) or 0)
    total_refs = int(getattr(user, 'total_referrals', 0) or 0)
    first_name = user.full_name.split()[0] if user.full_name else "user"

    return (
        "🛍 Rain Store\n"
        "Premium Digital Marketplace\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👋 Welcome back, {safe(first_name)}\n\n"
        "💎 Standard Plan\n\n"
        f"💰 Wallet: ${balance_str}\n"
        f"📦 Orders: {total_orders}\n"
        f"🎁 Rewards: {total_refs}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<blockquote>"
        "✓ Verified Premium Products\n"
        "✓ Instant Delivery\n"
        "✓ Secure Payments\n"
        "✓ Dedicated Customer Support"
        "</blockquote>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "<blockquote>"
        "🛒 Shop \"Browse premium digital products\"\n"
        "💰 Deposit \"Top up your wallet instantly\"\n"
        "👤 Profile \"Manage your account & wallet\"\n"
        "📦 Orders \"View purchases & product keys\"\n"
        "📞 Support \"Get help from our support team\""
        "</blockquote>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📢 Stay Updated: @Popeye7707\n\n"
        "👇 Tap a button below to get started."
    )


# =====================================================
# /admin COMMAND
# =====================================================

@router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    dashboard = await asyncio.to_thread(
        _load_admin_dashboard, message.from_user.full_name, message.from_user.id
    )
    await answer_card(message,
        dashboard,
        reply_markup=_get_admin_panel_with_providers(),
        parse_mode="HTML",
    )


# =====================================================
# ADMIN PANEL
# =====================================================

@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    # Clear Telegram's button spinner before the remote dashboard count query.
    await callback.answer()
    await state.clear()
    text = await asyncio.to_thread(
        _load_admin_dashboard, callback.from_user.full_name, callback.from_user.id
    )
    await _safe_edit_text(
        callback.message,
        text,
        reply_markup=_get_admin_panel_with_providers(),
        parse_mode="HTML"
    )


def _load_admin_audit_page(page: int, page_size: int = 10) -> tuple[list, int]:
    db = SessionLocal()
    try:
        query = db.query(AdminAuditLog).order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
        total = query.count()
        page = max(0, min(page, max((total - 1) // page_size, 0)))
        rows = query.offset(page * page_size).limit(page_size).all()
        return ([{
            "id": row.id,
            "admin_id": row.admin_telegram_id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "summary": row.summary,
            "created_at": row.created_at,
        } for row in rows], total)
    finally:
        db.close()


@router.callback_query(F.data == "admin_audit")
@router.callback_query(F.data.startswith("admin_audit_page_"))
async def admin_audit(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        page = int(callback.data.rsplit("_", 1)[1]) if callback.data.startswith("admin_audit_page_") else 0
    except ValueError:
        page = 0
    page_size = 10
    rows, total = await asyncio.to_thread(_load_admin_audit_page, page, page_size)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    lines = [f"🧾 <b>ADMIN ACTIVITY LOG</b> · {page + 1}/{total_pages}\n"]
    if not rows:
        lines.append("No tracked admin changes yet.")
    for row in rows:
        when = row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row["created_at"] else "time unavailable"
        summary = html_escape(row["summary"] or "")
        entity = html_escape(f"{row['entity_type']} #{row['entity_id']}".strip())
        action = html_escape(row["action"])
        lines.append(
            f"<b>{action}</b> · {entity}\n"
            f"Admin <code>{row['admin_id']}</code> · {when}\n"
            f"{summary}\n"
        )
    keyboard = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅ Older", callback_data=f"admin_audit_page_{page - 1}"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="Newer ➡", callback_data=f"admin_audit_page_{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton(text="⬅ Admin Panel", callback_data="admin_panel")])
    await callback.answer()
    await _safe_edit_text(
        callback.message,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data == "manager_web_access")
async def manager_web_access(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer(
            "Open the Admin Panel in a private chat to create the Manager Web link.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.set_state(ManagerWebState.waiting_password)
    await answer_card(callback,
        "🔐 <b>Manager Web access</b>\n\n"
        "Enter your manager password in this private chat to generate a one-time login link. "
        "Your password message will be deleted after it is received.",
        parse_mode="HTML",
        state=state,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✖ Cancel", callback_data="manager_web_cancel")
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data == "manager_web_cancel")
async def manager_web_cancel(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    if callback.message:
        cancelled_card = await answer_card(callback, "Manager Web link generation cancelled.", state=state)
        if cancelled_card:
            schedule_delete_card(cancelled_card, owner_id=callback.from_user.id, delay_seconds=900)
    await state.clear()
    await callback.answer()


@router.message(ManagerWebState.waiting_password)
async def manager_web_password(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in ADMIN_IDS or message.chat.type != "private":
        await state.clear()
        return

    password = (message.text or "").strip()
    card_data = await state.get_data()
    card_chat_id = int(card_data.get("_card_chat_id") or message.chat.id)
    card_message_id = int(card_data.get("_card_message_id") or 0)
    try:
        await message.delete()
    except Exception:
        pass

    submitted_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(submitted_hash, MAINTENANCE_PASSWORD_HASH):
        error_card = await answer_card(
            message,
            "❌ Incorrect password. No Manager Web link was created.",
            state=state,
        )
        if error_card:
            schedule_delete_card(error_card, owner_id=message.from_user.id, delay_seconds=900)
        await state.clear()
        return

    if card_message_id <= 0:
        await answer_card(
            message,
            "⚠️ The Manager Web card expired. Open Manager Web from the Admin Panel and try again.",
            state=state,
        )
        await state.clear()
        return

    try:
        token = await asyncio.to_thread(
            _create_manager_web_link,
            message.from_user.id,
            card_chat_id,
            card_message_id,
        )
    except Exception:
        logger.exception("Could not create one-time Manager Web login link")
        error_card = await answer_card(
            message,
            "⚠️ Could not create a Manager Web link. Check the database connection and try again.",
            state=state,
        )
        if error_card is not None:
            schedule_delete_card(error_card, owner_id=message.from_user.id, delay_seconds=900)
        await state.clear()
        return

    login_url = f"{MANAGER_WEB_URL}/login/bot#{token}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Open Manager Web", url=login_url)]
        ]
    )
    link_card = await answer_card(message,
        "🔐 <b>Private Manager Web link</b>\n\n"
        "This link is unique, expires in 2 minutes, and works once. Opening it signs you in without another Telegram verification prompt. "
        "It starts a 60-minute manager session. The button is removed as soon as the link is used or expires.",
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
        state=state,
    )
    if link_card is not None:
        schedule_delete_card(link_card, owner_id=message.from_user.id, delay_seconds=900)
        try:
            await asyncio.to_thread(
                _update_manager_web_link_card,
                token,
                link_card.chat.id,
                link_card.message_id,
            )
        except Exception:
            logger.exception("Could not attach the final Telegram card to the Manager Web link")
    await state.clear()



# ============================================================
# PROVIDER MANAGEMENT
# ============================================================

@router.callback_query(F.data.in_({"admin_providers", "admin:providers"}))
async def admin_providers(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()

    text = (
        "🏪 <b>PROVIDER MANAGEMENT</b>\n\n"
        "<code>┌──(root㉿Rain)-[/providers]</code>\n"
        "<code>└─# sudo stockctl provider --status</code>\n"
        "<code>[SYS] Provider Subsystem Ready</code>\n\n"
        "Manage API providers, credentials, and supplier connections.\n\n"
        "👇 Select an action below:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Providers", callback_data="admin_provider_list")],
            [InlineKeyboardButton(text="➕ Add Provider", callback_data="admin_provider_add")],
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_providers")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")]
        ]
    )

    await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"admin_provider_list", "admin:providers:list"}))
async def admin_provider_list(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()
    db = SessionLocal()
    try:
        providers = db.query(Provider).order_by(Provider.id.asc()).all()

        if not providers:
            text = (
                "🏪 <b>PROVIDER LIST</b>\n\n"
                "⚠️ <i>No providers configured in database.</i>\n\n"
                "Click ➕ <b>Add Provider</b> to configure your first provider."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Add Provider", callback_data="admin_provider_add")],
                    [InlineKeyboardButton(text="🔙 Back", callback_data="admin_providers")]
                ]
            )
            await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
            await callback.answer()
            return

        lines = ["🏪 <b>CONFIGURED PROVIDERS</b>\n"]
        buttons = []

        for p in providers:
            status_str = "🟢 Active" if p.is_active else "🔴 Inactive"
            lines.append(
                f"• <b>{safe(p.name)}</b> (<code>{safe(p.provider_key)}</code>)\n"
                f"  URL: <code>{safe(p.base_url)}</code>\n"
                f"  Type: <code>{safe(p.api_type)}</code> | Auth: <code>{safe(p.auth_type)}</code>\n"
                f"  Status: {status_str}\n"
            )

            buttons.append([
                InlineKeyboardButton(text=f"✏️ Edit {p.name[:12]}", callback_data=f"admin_provider_edit_{p.id}"),
                InlineKeyboardButton(
                    text="🔴 Disable" if p.is_active else "🟢 Enable",
                    callback_data=f"admin_provider_toggle_{p.id}"
                ),
                InlineKeyboardButton(text="🗑 Delete", callback_data=f"admin_provider_delete_{p.id}")
            ])

        buttons.append([InlineKeyboardButton(text="➕ Add Provider", callback_data="admin_provider_add")])
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_providers")])

        await _safe_edit_text(
            callback.message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML"
        )
    finally:
        db.close()

    await callback.answer()


@router.callback_query(F.data.in_({"admin_provider_add", "admin:providers:add"}))
async def admin_provider_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()
    text = (
        "🏪 <b>ADD PROVIDER</b>\n\n"
        "How do you want to configure this provider?"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Automatic Setup", callback_data="admin_provider_auto")],
            [InlineKeyboardButton(text="🛠 Manual Setup", callback_data="admin_provider_manual")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_list")]
        ]
    )

    await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"admin_provider_auto", "admin:provider:auto"}))
async def admin_provider_auto(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    text = (
        "🤖 <b>AUTOMATIC API SETUP</b>\n\n"
        "🤖 Automatic API setup is not available yet.\n\n"
        "Please use 🛠 <b>Manual Setup</b> to add your provider details."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Manual Setup", callback_data="admin_provider_manual")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_provider_add")]
        ]
    )

    await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# ============================================================
# MANUAL SETUP FSM FLOW
# ============================================================

@router.callback_query(F.data.in_({"admin_provider_manual", "admin:provider:manual"}))
async def start_provider_manual_setup(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()
    await state.set_state(ProviderSetupState.waiting_name)

    text = (
        "🛠 <b>MANUAL PROVIDER SETUP (Step 1/7)</b>\n\n"
        "Please send the <b>Provider Name</b>.\n\n"
        "<i>Example: Noman Shop Bot or Supplier X</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]
        ]
    )

    await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.message(ProviderSetupState.waiting_name)
async def process_provider_name(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    name = (message.text or "").strip()
    if not name or len(name) < 2:
        await answer_card(message, "❌ <b>Invalid Name!</b> Please send a valid provider name (at least 2 characters).",
                             parse_mode="HTML")
        return

    await state.update_data(name=name)
    await state.set_state(ProviderSetupState.waiting_key)

    default_key = name.lower().replace(" ", "_").replace("-", "_")
    default_key = "".join(c for c in default_key if c.isalnum() or c == "_")

    await answer_card(message,
        f"✅ <b>Name:</b> {safe(name)}\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 2/7)</b>\n\n"
        "Please send a unique <b>Provider Key</b> (slug identifier).\n\n"
        f"<i>Suggested: <code>{safe(default_key)}</code></i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]]
        )
    )


@router.message(ProviderSetupState.waiting_key)
async def process_provider_key(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    raw_key = (message.text or "").strip().lower()
    clean_key = "".join(c for c in raw_key if c.isalnum() or c in ("_", "-"))

    if not clean_key:
        await answer_card(message, "❌ <b>Invalid Key!</b> Key must contain letters, numbers, hyphens, or underscores.",
                             parse_mode="HTML")
        return

    db = SessionLocal()
    try:
        existing = db.query(Provider).filter(Provider.provider_key == clean_key).first()
        if existing:
            await answer_card(message, "❌ <b>Provider key already exists!</b> Please send a different unique provider key.",
                                 parse_mode="HTML")
            return
    finally:
        db.close()

    await state.update_data(provider_key=clean_key)
    await state.set_state(ProviderSetupState.waiting_base_url)

    await answer_card(message,
        f"✅ <b>Provider Key:</b> <code>{safe(clean_key)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 3/7)</b>\n\n"
        "Please send the <b>Base URL</b> of the provider API.\n\n"
        "<i>Example: https://api.supplier.com</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]]
        )
    )


@router.message(ProviderSetupState.waiting_base_url)
async def process_provider_base_url(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    url = (message.text or "").strip().rstrip("/")
    if not _validate_base_url(url):
        await answer_card(message,
            "❌ <b>Invalid URL!</b> Base URL must start with <code>http://</code> or <code>https://</code>.",
            parse_mode="HTML")
        return

    await state.update_data(base_url=url)
    await state.set_state(ProviderSetupState.waiting_api_type)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="excalibur", callback_data="set_apitype_excalibur"),
                InlineKeyboardButton(text="generic", callback_data="set_apitype_generic"),
                InlineKeyboardButton(text="rest", callback_data="set_apitype_rest")
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]
        ]
    )

    await answer_card(message,
        f"✅ <b>Base URL:</b> <code>{safe(url)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 4/7)</b>\n\n"
        "Select or send the <b>API Type</b>:\n"
        "• <code>excalibur</code> (Excalibur reseller API format)\n"
        "• <code>generic</code> (Standard REST API format)\n"
        "• <code>rest</code> (Custom REST format)",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("set_apitype_"))
async def process_provider_api_type_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    api_type = callback.data.removeprefix("set_apitype_")
    await state.update_data(api_type=api_type)
    await state.set_state(ProviderSetupState.waiting_auth_type)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="header (X-API-Key)", callback_data="set_authtype_header"),
                InlineKeyboardButton(text="bearer (Bearer Token)", callback_data="set_authtype_bearer")
            ],
            [
                InlineKeyboardButton(text="query (URL Param)", callback_data="set_authtype_query"),
                InlineKeyboardButton(text="HMAC (Key + Secret)", callback_data="set_authtype_hmac")
            ],
            [InlineKeyboardButton(text="WmEmail Open (API Key)", callback_data="set_authtype_wmemail_open")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]
        ]
    )

    await _safe_edit_text(
        callback.message,
        f"✅ <b>API Type:</b> <code>{safe(api_type)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 5/7)</b>\n\n"
        "Select or send the <b>Authentication Type</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()


@router.message(ProviderSetupState.waiting_api_type)
async def process_provider_api_type_msg(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    api_type = (message.text or "").strip().lower() or "generic"
    await state.update_data(api_type=api_type)
    await state.set_state(ProviderSetupState.waiting_auth_type)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="header (X-API-Key)", callback_data="set_authtype_header"),
                InlineKeyboardButton(text="bearer (Bearer Token)", callback_data="set_authtype_bearer")
            ],
            [
                InlineKeyboardButton(text="query (URL Param)", callback_data="set_authtype_query"),
                InlineKeyboardButton(text="HMAC (Key + Secret)", callback_data="set_authtype_hmac")
            ],
            [InlineKeyboardButton(text="WmEmail Open (API Key)", callback_data="set_authtype_wmemail_open")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]
        ]
    )

    await answer_card(message,
        f"✅ <b>API Type:</b> <code>{safe(api_type)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 5/7)</b>\n\n"
        "Select or send the <b>Authentication Type</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("set_authtype_"))
async def process_provider_auth_type_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    auth_type = callback.data.removeprefix("set_authtype_")
    await state.update_data(auth_type=auth_type)
    await state.set_state(ProviderSetupState.waiting_api_key)

    await _safe_edit_text(
        callback.message,
        f"✅ <b>Auth Type:</b> <code>{safe(auth_type)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 6/7)</b>\n\n"
        "Please send the <b>API Key / Token</b> for this provider.\n\n"
        "<i>Send 'none' or 'skip' if no key is required.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]]
        )
    )
    await callback.answer()


@router.message(ProviderSetupState.waiting_auth_type)
async def process_provider_auth_type_msg(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    auth_type = (message.text or "").strip().lower() or "api_key"
    await state.update_data(auth_type=auth_type)
    await state.set_state(ProviderSetupState.waiting_api_key)

    await answer_card(message,
        f"✅ <b>Auth Type:</b> <code>{safe(auth_type)}</code>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 6/7)</b>\n\n"
        "Please send the <b>API Key / Token</b> for this provider.\n\n"
        "<i>Send 'none' or 'skip' if no key is required.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")]]
        )
    )


@router.message(ProviderSetupState.waiting_api_key)
async def process_provider_api_key(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    raw_key = (message.text or "").strip()
    api_key = None if raw_key.lower() in ("none", "skip", "") else raw_key

    await state.update_data(api_key=api_key)
    data = await state.get_data()
    if data.get("auth_type") == "hmac":
        if not api_key:
            await answer_card(message, "HMAC authentication requires an API key.")
            return

        await state.set_state(ProviderSetupState.waiting_api_secret)
        await answer_card(message,
            "API key saved securely. Send the API secret for this provider.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="admin_provider_cancel")]]
            ),
        )
        return

    await state.update_data(api_secret=None)
    await state.set_state(ProviderSetupState.waiting_configuration)

    await answer_card(message,
        "✅ <b>API Key saved securely (hidden)</b>\n\n"
        "🛠 <b>MANUAL PROVIDER SETUP (Step 7/7)</b>\n\n"
        "Send additional JSON configuration (optional) or send <b>skip</b>:\n\n"
        "<i>Example:</i> <code>{\"products_endpoint\": \"/api/v1/products\"}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⏩ Skip Configuration", callback_data="skip_provider_config")]]
        )
    )


@router.message(ProviderSetupState.waiting_api_secret)
async def process_provider_api_secret(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    api_secret = (message.text or "").strip()
    if not api_secret or api_secret.lower() in ("none", "skip"):
        await answer_card(message, "HMAC authentication requires an API secret.")
        return

    await state.update_data(api_secret=api_secret)
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(ProviderSetupState.waiting_configuration)
    await answer_card(message,
        "API secret saved securely. Send optional JSON configuration or send skip.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Skip Configuration", callback_data="skip_provider_config")]]
        ),
    )


@router.callback_query(F.data == "skip_provider_config")
async def skip_provider_config(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.update_data(configuration=None)
    await show_provider_preview(callback, state)


@router.message(ProviderSetupState.waiting_configuration)
async def process_provider_configuration(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    raw_text = (message.text or "").strip()
    if raw_text.lower() in ("skip", "none", ""):
        config_val = None
    else:
        try:
            json.loads(raw_text)
            config_val = raw_text
        except Exception:
            await answer_card(message, "❌ <b>Invalid JSON format!</b> Please send valid JSON or send 'skip'.",
                                 parse_mode="HTML")
            return

    await state.update_data(configuration=config_val)
    await show_provider_preview(message, state)


async def show_provider_preview(target: Message | CallbackQuery, state: FSMContext):
    data = await state.get_data()

    preview_text = (
        "🏪 <b>PROVIDER PREVIEW</b>\n\n"
        f"<b>Name:</b> {safe(data.get('name', ''))}\n"
        f"<b>Key:</b> <code>{safe(data.get('provider_key', ''))}</code>\n"
        f"<b>Base URL:</b> <code>{safe(data.get('base_url', ''))}</code>\n"
        f"<b>API Type:</b> <code>{safe(data.get('api_type', 'generic'))}</code>\n"
        f"<b>Auth Type:</b> <code>{safe(data.get('auth_type', 'api_key'))}</code>\n"
        f"<b>API Key:</b> ********\n"
        f"<b>API Secret:</b> {'********' if data.get('api_secret') else 'Not used'}\n"
        f"<b>Configuration:</b> <code>{safe(data.get('configuration') or 'None')}</code>\n\n"
        "Save this provider?"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Save", callback_data="admin_provider_save_confirm"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_cancel")
            ]
        ]
    )

    if isinstance(target, CallbackQuery):
        await _safe_edit_text(target.message, preview_text, reply_markup=keyboard, parse_mode="HTML")
        await answer_card(target, )
    else:
        await answer_card(target, preview_text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "admin_provider_save_confirm")
async def save_provider_to_db(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    data = await state.get_data()
    if data.get("auth_type") == "hmac" and not data.get("api_secret"):
        await callback.answer("HMAC authentication requires an API secret.", show_alert=True)
        return

    db = SessionLocal()
    try:
        provider = Provider(
            name=data["name"],
            provider_key=data["provider_key"],
            base_url=data["base_url"],
            api_type=data.get("api_type", "generic"),
            auth_type=data.get("auth_type", "api_key"),
            api_key=data.get("api_key"),
            api_secret=data.get("api_secret"),
            configuration=data.get("configuration"),
            is_active=True,
        )
        db.add(provider)
        db.commit()
        db.refresh(provider)
        prov_name = provider.name
    except Exception as e:
        db.rollback()
        logger.exception("Failed to save provider to database")
        await _safe_edit_text(
            callback.message,
            f"❌ <b>Database Error:</b> Could not save provider.\n<code>{safe(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Provider List", callback_data="admin_provider_list")]])
        )
        await state.clear()
        return
    finally:
        db.close()

    await state.clear()
    await _safe_edit_text(
        callback.message,
        f"✅ <b>Provider Saved Successfully!</b>\n\nProvider <b>{safe(prov_name)}</b> is now ready for use.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📋 Provider List", callback_data="admin_provider_list")]]
        )
    )
    await callback.answer()


@router.callback_query(F.data == "admin_provider_cancel")
async def cancel_provider_setup(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()
    await callback.answer("❌ Provider setup cancelled.")
    await admin_provider_list(callback, state)


# ============================================================
# TOGGLE / DELETE / EDIT PROVIDERS
# ============================================================

@router.callback_query(F.data.startswith("admin_provider_toggle_"))
async def toggle_provider(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    provider_id = int(callback.data.removeprefix("admin_provider_toggle_"))
    db = SessionLocal()
    try:
        p = db.query(Provider).filter(Provider.id == provider_id).first()
        if not p:
            await callback.answer("Provider not found.", show_alert=True)
            return

        p.is_active = not p.is_active
        db.commit()
        status_msg = f"🟢 {p.name} Enabled" if p.is_active else f"🔴 {p.name} Disabled"
    finally:
        db.close()

    await callback.answer(status_msg, show_alert=True)
    await admin_provider_list(callback, state)


@router.callback_query(F.data.startswith("admin_provider_delete_"))
async def delete_provider_confirm(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    provider_id = int(callback.data.removeprefix("admin_provider_delete_"))
    db = SessionLocal()
    try:
        p = db.query(Provider).filter(Provider.id == provider_id).first()
        if not p:
            await callback.answer("Provider not found.", show_alert=True)
            return

        text = (
            "⚠️ <b>Delete Provider?</b>\n\n"
            f"Provider: <b>{safe(p.name)}</b> (<code>{safe(p.provider_key)}</code>)\n\n"
            "This action cannot be undone."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Confirm Delete", callback_data=f"admin_provider_confirmdel_{p.id}"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data="admin_provider_list")
                ]
            ]
        )

        await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    finally:
        db.close()

    await callback.answer()


@router.callback_query(F.data.startswith("admin_provider_confirmdel_"))
async def delete_provider_execute(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    provider_id = int(callback.data.removeprefix("admin_provider_confirmdel_"))
    db = SessionLocal()
    try:
        p = db.query(Provider).filter(Provider.id == provider_id).first()
        if not p:
            await callback.answer("Provider not found.", show_alert=True)
            return

        conds = []
        if hasattr(Product, "provider_id"):
            conds.append(Product.provider_id == p.id)
        if hasattr(Product, "reseller_name"):
            conds.append(Product.reseller_name == p.name)
        if hasattr(Product, "reseller_id"):
            conds.append(Product.reseller_id == str(p.id))
            conds.append(Product.reseller_id == p.provider_key)

        products_using = db.query(Product).filter(or_(*conds)).first() if conds else None

        if products_using:
            await _safe_edit_text(
                callback.message,
                f"❌ <b>Cannot Delete Provider</b>\n\n"
                f"This provider (<b>{safe(p.name)}</b>) is still used by active products in store.\n\n"
                f"Please remove or reassign those products first.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Provider List", callback_data="admin_provider_list")]])
            )
            await callback.answer()
            return

        db.delete(p)
        db.commit()
        await callback.answer("✅ Provider deleted successfully.", show_alert=True)
    finally:
        db.close()

    await admin_provider_list(callback, state)


@router.callback_query(F.data.startswith("admin_provider_edit_"))
async def edit_provider_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    provider_id = int(callback.data.removeprefix("admin_provider_edit_"))
    db = SessionLocal()
    try:
        p = db.query(Provider).filter(Provider.id == provider_id).first()
        if not p:
            await callback.answer("Provider not found.", show_alert=True)
            return

        text = (
            f"✏️ <b>EDIT PROVIDER: {safe(p.name)}</b>\n\n"
            f"<b>Key:</b> <code>{safe(p.provider_key)}</code>\n"
            f"<b>Base URL:</b> <code>{safe(p.base_url)}</code>\n"
            f"<b>API Type:</b> <code>{safe(p.api_type)}</code>\n"
            f"<b>Auth Type:</b> <code>{safe(p.auth_type)}</code>\n"
            f"<b>API Key:</b> ********\n"
            f"<b>API Secret:</b> {'********' if p.api_secret else 'Not used'}\n\n"
            "Select a field to edit:"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Name", callback_data=f"pedit_field_name_{p.id}"),
                    InlineKeyboardButton(text="Base URL", callback_data=f"pedit_field_base_url_{p.id}")
                ],
                [
                    InlineKeyboardButton(text="API Type", callback_data=f"pedit_field_api_type_{p.id}"),
                    InlineKeyboardButton(text="Auth Type", callback_data=f"pedit_field_auth_type_{p.id}")
                ],
                [
                    InlineKeyboardButton(text="API Key / Token", callback_data=f"pedit_field_api_key_{p.id}"),
                    InlineKeyboardButton(text="API Secret", callback_data=f"pedit_field_api_secret_{p.id}")
                ],
                [InlineKeyboardButton(text="Configuration", callback_data=f"pedit_field_configuration_{p.id}")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_provider_list")]
            ]
        )

        await _safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="HTML")
    finally:
        db.close()

    await callback.answer()


@router.callback_query(F.data.startswith("pedit_field_"))
async def prompt_edit_provider_field(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    parts = callback.data.split("_")
    field_name = "_".join(parts[2:-1])
    provider_id = int(parts[-1])

    await state.update_data(edit_provider_id=provider_id, edit_field=field_name)
    await state.set_state(ProviderEditState.waiting_field_value)

    prompt = f"Send the new value for <b>{field_name}</b>:"
    if field_name in ("api_key", "api_secret"):
        prompt += "\n\n<i>Current credential is hidden. Send a new value or 'keep' to keep it unchanged.</i>"

    await _safe_edit_text(
        callback.message,
        prompt,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin_provider_edit_{provider_id}")]])
    )
    await callback.answer()


@router.message(ProviderEditState.waiting_field_value)
async def process_edit_provider_field_save(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    data = await state.get_data()
    provider_id = data.get("edit_provider_id")
    field_name = data.get("edit_field")
    new_val = (message.text or "").strip()

    if field_name == "auth_type" and new_val.lower() in {
        "hmac", "hmac_sha256", "dujiao_hmac", "dujiao-next", "dujiao_next", "signature"
    }:
        db = SessionLocal()
        try:
            provider = db.query(Provider).filter(Provider.id == provider_id).first()
            if provider and not provider.api_secret:
                await answer_card(message, "Add the provider API secret before switching to HMAC authentication.")
                return
        finally:
            db.close()

    if field_name in ("api_key", "api_secret"):
        try:
            await message.delete()
        except Exception:
            pass

    if field_name in ("api_key", "api_secret") and new_val.lower() == "keep":
        await state.clear()
        await answer_card(message, "Existing API key kept.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📋 Provider List", callback_data="admin_provider_list")]]))
        return

    db = SessionLocal()
    try:
        p = db.query(Provider).filter(Provider.id == provider_id).first()
        if not p:
            await answer_card(message, "Provider not found.")
            await state.clear()
            return

        if field_name == "base_url" and not _validate_base_url(new_val):
            await answer_card(message,
                "❌ Invalid URL! Base URL must start with <code>http://</code> or <code>https://</code>.",
                parse_mode="HTML")
            return

        if field_name == "configuration" and new_val.lower() not in ("none", "skip", "null"):
            try:
                json.loads(new_val)
            except Exception:
                await answer_card(message, "❌ Invalid JSON format for configuration.", parse_mode="HTML")
                return

        setattr(p, field_name, new_val)
        db.commit()
        await answer_card(message,
            f"✅ Provider <b>{field_name}</b> updated!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 Provider List", callback_data="admin_provider_list")]])
        )
    finally:
        db.close()
        await state.clear()


# =====================================================
# USERS
# =====================================================

@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id.desc()).limit(50).all()
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"👤 {u.full_name} | {u.telegram_id}",
                    callback_data=f"view_user_{u.id}"
                )
            ]
            for u in users
        ]
        keyboard.append([
            InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")
        ])

        await _safe_edit_text(
            callback.message,
            f"👥 <b>Users ({len(users)})</b>\n\n<i>Showing latest 50</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("view_user_"))
async def view_user(callback: CallbackQuery):
    uid = int(callback.data.split("_")[2])
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        if not user:
            await callback.answer("User not found.", show_alert=True)
            return

        balance_raw = getattr(user, 'balance_display', getattr(user, 'balance', 0))
        balance_str = _format_balance(balance_raw)

        text = (
            "╔════════════════════════════╗\n"
            "║      👤 USER DETAILS        ║\n"
            "╚════════════════════════════╝\n\n"
            f"👤 <b>Name:</b> {safe(user.full_name)}\n"
            f"📝 <b>Username:</b> @{safe(user.username or 'None')}\n"
            f"🆔 <b>Telegram ID:</b> <code>{safe(str(user.telegram_id))}</code>\n\n"
            "────────────────────────────\n"
            f"💰 <b>Balance:</b> ${balance_str}\n"
            f"🎁 <b>Referral Earnings:</b> ${float(user.referral_earnings):.2f}\n"
            f"👥 <b>Referrals:</b> {user.total_referrals}\n\n"
            "────────────────────────────\n"
            f"🛒 <b>Orders:</b> {user.total_orders}\n"
            f"💸 <b>Total Spent:</b> ${float(user.total_spent):.2f}\n"
            f"📥 <b>Total Deposited:</b> ${float(user.total_deposited):.2f}\n\n"
            "────────────────────────────\n"
            f"🚫 <b>Banned:</b> {'<b>YES</b> 🚫' if user.is_banned else 'No 🟢'}"
        )

        await _safe_edit_text(
            callback.message,
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="➕ Add / Remove Balance",
                            callback_data=f"user_balance_adjust_{user.id}"
                        ),
                        InlineKeyboardButton(
                            text="✏️ Set Balance",
                            callback_data=f"user_balance_set_{user.id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🛒 Purchase History",
                            callback_data=f"user_orders_{user.id}_0"
                        ),
                        InlineKeyboardButton(
                            text="📥 Deposit History",
                            callback_data=f"user_deposits_{user.id}_0"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="📦 Product Summary",
                            callback_data=f"user_products_{user.id}"
                        ),
                        InlineKeyboardButton(
                            text="✅ Unban" if user.is_banned else "🚫 Ban",
                            callback_data=f"user_ban_{user.id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔙 Back to Users",
                            callback_data="admin_users"
                        )
                    ]
                ]
            )
        )
    finally:
        db.close()
    await callback.answer()


# =====================================================
# PRODUCTS
# =====================================================

@router.callback_query(F.data == "admin_products")
async def admin_products(callback: CallbackQuery):
    await callback.answer()
    products = await asyncio.to_thread(_load_admin_product_rows)
    keyboard = [
        [InlineKeyboardButton(text="➕ New Product", callback_data="create_product")]
    ]
    for product_id, active, icon, name, stock in products:
        status = "🟢" if active else "🔴"
        keyboard.append([
            InlineKeyboardButton(
                text=f"#{product_id} {status} {icon} {name} ({stock})",
                callback_data=f"manage_{product_id}",
            )
        ])
    keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")])
    await _safe_edit_text(
        callback.message,
        "📦 <b>Product Management</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


# =====================================================
# STATISTICS
# =====================================================

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()
    users, products, orders, deposits, tickets, revenue = await asyncio.to_thread(
        _load_admin_stats
    )

    text = (
        "╔════════════════════════════╗\n"
        "║       📊 STATISTICS        ║\n"
        "╚════════════════════════════╝\n\n"
        f"👥 <b>Users:</b> {users}\n"
        f"📦 <b>Products:</b> {products}\n"
        f"🛒 <b>Orders:</b> {orders}\n"
        f"💰 <b>Deposits:</b> {deposits}\n"
        f"🎫 <b>Tickets:</b> {tickets}\n\n"
        "════════════════════════════\n"
        f"💵 <b>Revenue:</b> ${float(revenue or 0):.2f}"
    )

    await _safe_edit_text(
        callback.message,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")]
            ]
        )
    )


# =====================================================
# BROADCAST — FULLY FIXED
# =====================================================

@router.callback_query(F.data == "admin_broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext):
    """Start broadcast mode — sets FSM state and waits for message."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    # Set state to wait for broadcast message
    await state.set_state(BroadcastState.waiting_message)

    await _safe_edit_text(
        callback.message,
        "📢 <b>Broadcast Mode Activated</b>\n\n"
        "✏️ Send me the message you want to broadcast to <b>ALL users</b>.\n\n"
        "📝 <i>You can send text, photos, videos, documents — anything!</i>\n\n"
        "⚠️ <i>This will be forwarded to every user in the database.</i>\n\n"
        "👇 Click Cancel to abort.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel Broadcast", callback_data="cancel_broadcast")]
            ]
        )
    )

    await callback.answer("📢 Broadcast mode activated — send your message now.")


@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    """Cancel the broadcast and return to admin panel."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()

    db = SessionLocal()
    try:
        text = _build_admin_dashboard(db, callback.from_user.full_name, callback.from_user.id)
        await _safe_edit_text(
            callback.message,
            text,
            reply_markup=_get_admin_panel_with_providers(),
            parse_mode="HTML"
        )
    finally:
        db.close()

    await callback.answer("❌ Broadcast cancelled.")


@router.message(BroadcastState.waiting_message)
async def send_broadcast(message: Message, state: FSMContext):
    """Handle the broadcast message and send to all users."""
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        await answer_card(message, "⛔ Access denied. You are not an admin.")
        return

    # Send initial status message
    status_msg = await answer_card(message,
        "📢 <b>Broadcasting...</b>\n\n⏳ Fetching users from database...",
        parse_mode="HTML"
    )

    db = SessionLocal()
    try:
        users = db.query(User.telegram_id).all()
        user_ids = [x[0] for x in users]
    finally:
        db.close()

    if not user_ids:
        await status_msg.edit_text("⚠️ <b>No users found in database!</b>", parse_mode="HTML")
        await state.clear()
        return

    total = len(user_ids)
    sent = 0
    failed = 0
    blocked = 0
    deactivated = 0
    not_started = 0
    unknown_error = 0
    error_details = []

    # Update status message
    await status_msg.edit_text(
        f"📢 <b>Broadcasting...</b>\n\n"
        f"📊 Total users: {total}\n"
        f"✅ Sent: 0\n"
        f"❌ Failed: 0\n\n"
        f"⏳ Starting broadcast...",
        parse_mode="HTML"
    )

    # Send to each user with detailed error tracking
    for i, user_id in enumerate(user_ids, 1):
        try:
            await message.copy_to(chat_id=user_id)
            sent += 1

            # Small delay to avoid hitting rate limits
            if i % 5 == 0:
                await asyncio.sleep(0.1)

        except Exception as e:
            error_str = str(e).lower()

            if "bot was blocked by the user" in error_str or "blocked" in error_str:
                blocked += 1
            elif "user is deactivated" in error_str or "deactivated" in error_str:
                deactivated += 1
            elif "chat not found" in error_str:
                not_started += 1
            elif "bot can't initiate conversation" in error_str:
                not_started += 1
            else:
                failed += 1
                unknown_error += 1
                if len(error_details) < 5:
                    error_details.append(f"UID {user_id}: {str(e)[:100]}")

        # Update progress every 5 users or on last user
        if i % 5 == 0 or i == total:
            try:
                progress_text = (
                    f"📢 <b>Broadcasting...</b>\n\n"
                    f"📊 Progress: {i}/{total}\n"
                    f"✅ Sent: {sent}\n"
                    f"❌ Failed: {failed}\n"
                    f"🚫 Blocked: {blocked}\n\n"
                    f"⏳ Still sending..."
                )
                await status_msg.edit_text(progress_text, parse_mode="HTML")
            except Exception:
                pass

    # Calculate stats
    total_failed = blocked + deactivated + not_started + unknown_error
    success_rate = (sent / total * 100) if total > 0 else 0

    # Build detailed result message
    result_text = (
        f"📢 <b>✅ Broadcast Complete!</b>\n\n"
        f"<code>═══════════════════════</code>\n"
        f"📊 <b>Total Users in DB:</b> {total}\n"
        f"<code>═══════════════════════</code>\n"
        f"✅ <b>Successfully Sent:</b> {sent}\n"
        f"📈 <b>Success Rate:</b> {success_rate:.1f}%\n"
        f"<code>═══════════════════════</code>\n"
        f"❌ <b>Failed to Deliver:</b> {total_failed}\n"
        f"  ├ 🚫 <b>Blocked Bot:</b> {blocked}\n"
        f"  ├ 💀 <b>Deactivated Account:</b> {deactivated}\n"
        f"  ├ 🔇 <b>Never Started Bot:</b> {not_started}\n"
        f"  └ ⚠️ <b>Unknown Error:</b> {unknown_error}\n"
        f"<code>═══════════════════════</code>\n\n"
    )

    if blocked > 0 or deactivated > 0 or not_started > 0:
        result_text += "<b>ℹ️ Why so many failed?</b>\n"
        if not_started > 0:
            result_text += "• <b>Never started:</b> Users who haven't sent /start to the bot cannot receive messages (Telegram API restriction)\n"
        if blocked > 0:
            result_text += "• <b>Blocked:</b> Users who blocked the bot\n"
        if deactivated > 0:
            result_text += "• <b>Deactivated:</b> Users who deleted their Telegram account\n"
        result_text += "\n"

    if error_details:
        result_text += (
            f"<b>⚠️ Unknown Errors (first {len(error_details)}):</b>\n"
            f"<code>{chr(10).join(error_details)}</code>\n\n"
        )

    result_text += "<i>💡 Only users who have started the bot and not blocked it can receive broadcasts.</i>"

    await status_msg.edit_text(
        result_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Broadcast Again", callback_data="admin_broadcast"),
                    InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")
                ]
            ]
        )
    )

    await state.clear()


# =====================================================
# BACK TO USER MENU
# =====================================================

@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext):
    """Return the admin to the main menu with the current account summary."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return

    await state.clear()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == callback.from_user.id).first()
        if user:
            bot_username = await _get_bot_username(callback.bot)
            text = _build_start_welcome(
                user,
                callback.from_user.full_name,
                callback.from_user.id,
                callback.from_user.username,
                bot_username,
            )
        else:
            text = "<blockquote>🛍 <b>Welcome to Rain Store Bot!</b></blockquote>"
        await _safe_edit_text(
            callback.message,
            text,
            reply_markup=get_admin_main_menu(),
            parse_mode="HTML",
        )
    finally:
        db.close()
    await callback.answer()


# =====================================================
# USER BALANCE, ORDERS, DEPOSITS, PRODUCTS, BAN STATUS
# =====================================================

@router.callback_query(F.data.startswith("user_balance_adjust_"))
async def user_balance_adjust(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("user_balance_adjust_"))
    await state.set_state(AdminUserBalanceState.waiting_amount)
    await state.update_data(user_id=user_id, mode="adjust")
    await _safe_edit_text(
        callback.message,
        "💰 <b>Adjust Balance</b>\n\nSend an amount:\n"
        "<code>10</code> adds $10\n<code>-5.50</code> removes $5.50\n\n"
        "Maximum 8 decimal places.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"view_user_{user_id}")
        ]])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("user_balance_set_"))
async def user_balance_set(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("user_balance_set_"))
    await state.set_state(AdminUserBalanceState.waiting_amount)
    await state.update_data(user_id=user_id, mode="set")
    await _safe_edit_text(
        callback.message,
        "✏️ <b>Set Balance</b>\n\nSend the exact new balance, e.g. <code>25.50</code>.\n"
        "Negative balances are not allowed. Maximum 8 decimal places.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"view_user_{user_id}")
        ]])
    )
    await callback.answer()


@router.message(AdminUserBalanceState.waiting_amount)
async def save_user_balance(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        await answer_card(message, "⛔ Access denied.")
        return
    try:
        amount = Decimal((message.text or "").strip())
        if not amount.is_finite() or max(0, -amount.as_tuple().exponent) > 8:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await answer_card(message, "❌ Send a valid amount with at most 8 decimal places.")
        return

    data = await state.get_data()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == data.get("user_id")).first()
        if not user:
            await answer_card(message, "❌ User not found.")
            return
        old_balance = user.balance
        new_balance = amount if data.get("mode") == "set" else user.balance + amount
        if new_balance < Decimal("0"):
            await answer_card(message, f"❌ Balance cannot be negative. Current: {_money(user.balance)}")
            return
        user.balance = new_balance
        db.commit()
        await answer_card(message,
            f"✅ <b>Balance updated</b>\n\n👤 {safe(user.full_name)}\n"
            f"Previous: <b>{_money(old_balance)}</b>\nNew: <b>{_money(user.balance)}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="👤 Open User", callback_data=f"view_user_{user.id}")
            ]])
        )
    finally:
        db.close()
        await state.clear()


@router.callback_query(F.data.startswith("user_orders_"))
async def user_orders(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    _, _, user_id, page = callback.data.split("_")
    user_id, page = int(user_id), int(page)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("User not found.", show_alert=True)
            return
        query = db.query(Order).filter(Order.telegram_id == user.telegram_id).order_by(Order.created_at.desc())
        total = query.count()
        orders = query.offset(page * ACTIVITY_PER_PAGE).limit(ACTIVITY_PER_PAGE).all()
        lines = [f"🛒 <b>Purchase History</b>\n👤 {safe(user.full_name)}\n<i>{total} order(s)</i>\n"]
        for order in orders:
            status = "Refunded" if order.refunded else order.status.replace("_", " ").title()
            lines.append(
                f"<b>#{order.id}</b> {safe(order.product_name)}\n"
                f"Qty: {order.quantity} • {_money(order.amount)}\n"
                f"Status: {safe(status)} • {order.created_at.strftime('%d %b %Y, %H:%M')}\n"
            )
        if not orders:
            lines.append("No purchases found.")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"user_orders_{user_id}_{page - 1}"))
        if (page + 1) * ACTIVITY_PER_PAGE < total:
            nav.append(InlineKeyboardButton(text="Next ➡", callback_data=f"user_orders_{user_id}_{page + 1}"))
        rows = [nav] if nav else []
        rows.append([InlineKeyboardButton(text="🔙 User Details", callback_data=f"view_user_{user_id}")])
        await _safe_edit_text(callback.message, "\n".join(lines), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("user_deposits_"))
async def user_deposits(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    _, _, user_id, page = callback.data.split("_")
    user_id, page = int(user_id), int(page)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("User not found.", show_alert=True)
            return
        query = db.query(Deposit).filter(Deposit.telegram_id == user.telegram_id).order_by(Deposit.created_at.desc())
        total = query.count()
        deposits = query.offset(page * ACTIVITY_PER_PAGE).limit(ACTIVITY_PER_PAGE).all()
        lines = [f"📥 <b>Deposit History</b>\n👤 {safe(user.full_name)}\n<i>{total} deposit(s)</i>\n"]
        for deposit in deposits:
            lines.append(
                f"<b>#{deposit.id}</b> {_money(getattr(deposit, 'amount', Decimal('0')))}\n"
                f"Status: {safe(str(getattr(deposit, 'status', 'completed')).replace('_', ' ').title())} • "
                f"{deposit.created_at.strftime('%d %b %Y, %H:%M')}\n"
            )
        if not deposits:
            lines.append("No deposits found.")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"user_deposits_{user_id}_{page - 1}"))
        if (page + 1) * ACTIVITY_PER_PAGE < total:
            nav.append(InlineKeyboardButton(text="Next ➡", callback_data=f"user_deposits_{user_id}_{page + 1}"))
        rows = [nav] if nav else []
        rows.append([InlineKeyboardButton(text="🔙 User Details", callback_data=f"view_user_{user_id}")])
        await _safe_edit_text(callback.message, "\n".join(lines), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("user_products_"))
async def user_products(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("user_products_"))
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("User not found.", show_alert=True)
            return
        products = db.query(
            Order.product_name,
            func.count(Order.id).label("purchases"),
            func.coalesce(func.sum(Order.quantity), 0).label("quantity"),
            func.coalesce(func.sum(Order.amount), 0).label("paid"),
        ).filter(
            Order.telegram_id == user.telegram_id,
            Order.refunded.is_(False),
        ).group_by(Order.product_name).order_by(func.sum(Order.amount).desc()).all()
        lines = [f"📦 <b>Purchased Products</b>\n👤 {safe(user.full_name)}\n"]
        for product in products:
            lines.append(
                f"• <b>{safe(product.product_name)}</b>\n  Purchases: {product.purchases} • Qty: {product.quantity} • Paid: {_money(product.paid)}")
        if not products:
            lines.append("No completed product purchases found.")
        await _safe_edit_text(
            callback.message,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Purchase History", callback_data=f"user_orders_{user_id}_0")],
                [InlineKeyboardButton(text="🔙 User Details", callback_data=f"view_user_{user_id}")],
            ])
        )
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("user_ban_"))
async def user_ban_toggle(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("user_ban_"))
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await callback.answer("User not found.", show_alert=True)
            return
        user.is_banned = not user.is_banned
        db.commit()
        status = "🚫 User banned." if user.is_banned else "✅ User unbanned."
        user_name = safe(user.full_name)
    finally:
        db.close()
    await _safe_edit_text(
        callback.message,
        f"<b>{status}</b>\n\n👤 {user_name}\n"
        "The database status has been updated.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 Back to User Details", callback_data=f"view_user_{user_id}")
        ]]),
    )
    await callback.answer(status, show_alert=True)


# =====================================================
# CUSTOM USER RATES
# =====================================================

@router.callback_query(F.data == "custom_rate_menu")
async def custom_rate_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await _safe_edit_text(
        callback.message,
        "🏷 <b>CUSTOM RATES</b>\n\n"
        "Set a fixed USDT price for one user, on one product, or use Rate Control. "
        "Rate Control stores one price for each product and applies those prices to a user together.\n\n"
        "A product rate overrides an all-products rate.\n\n"
        "Stopping a rate restores normal pricing immediately, including on old product screens.",
        reply_markup=_custom_rate_menu_markup(), parse_mode="HTML",
    )
    await callback.answer()


def _rate_control_product_rows(products: list[Product], controls: dict[int, ProductRateControl]) -> list[
    list[InlineKeyboardButton]]:
    """Render compact buttons with their saved Rate Control prices."""
    rows = []
    for product in products:
        control = controls.get(product.id)
        label = f"#{product.id} {product.icon or '📦'} {product.name}"
        label += f" — ${control.price}" if control and control.is_active else " — not set"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"rate_control_product_{product.id}")])
    return rows


@router.callback_query(F.data == "rate_control_menu")
async def rate_control_menu(callback: CallbackQuery, state: FSMContext):
    """Set reusable per-product prices used by the All Products Rate Control option."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.is_active == True).order_by(Product.name.asc()).limit(80).all()
        controls = {item.product_id: item for item in db.query(ProductRateControl).all()}
    finally:
        db.close()
    if not products:
        await callback.answer("No active products found.", show_alert=True)
        return
    rows = _rate_control_product_rows(products, controls)
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="custom_rate_menu")])
    await _safe_edit_text(
        callback.message,
        "🎛 <b>RATE CONTROL</b>\n\n"
        "Choose a product, then send its reusable custom price in USDT. "
        "Users given <b>All Products (Rate Control)</b> receive these saved prices.\n\n"
        "Send <code>remove</code> while editing a product to remove its controlled price.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate_control_product_"))
async def rate_control_select_product(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Invalid product.", show_alert=True)
        return
    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        control = db.query(ProductRateControl).filter(ProductRateControl.product_id == product_id).first()
    finally:
        db.close()
    if not product or not product.is_active:
        await callback.answer("This product is unavailable.", show_alert=True)
        return
    await state.clear()
    await state.update_data(rate_control_product_id=product_id, rate_control_product_name=product.name)
    await state.set_state(CustomRateState.waiting_rate_control_price)
    current = f"${control.price}" if control and control.is_active else "not set"
    await _safe_edit_text(
        callback.message,
        f"🎛 <b>RATE CONTROL — {safe(product.name)}</b>\n\nCurrent controlled price: <b>{current}</b>\n\n"
        "Send the new price per unit in USDT.\n"
        "Example: <code>0.024</code>\n\n"
        "Send <code>remove</code> to delete this product's controlled price.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Back", callback_data="rate_control_menu")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="custom_rate_menu")],
        ]),
    )
    await callback.answer()


@router.message(CustomRateState.waiting_rate_control_price)
async def rate_control_save_price(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    data = await state.get_data()
    product_id = data.get("rate_control_product_id")
    product_name = data.get("rate_control_product_name", "product")
    if not product_id:
        await state.clear()
        await answer_card(message, "⚠️ Setup expired. Open Rate Control and try again.")
        return
    raw_value = (message.text or "").strip().lower()
    db = SessionLocal()
    try:
        control = db.query(ProductRateControl).filter(ProductRateControl.product_id == product_id).first()
        if raw_value == "remove":
            if control:
                db.delete(control)
                db.commit()
                invalidate_catalog_custom_prices_cache()
            await state.clear()
            await answer_card(message,
                f"✅ Rate Control price removed for <b>{safe(product_name)}</b>.\n"
                "Assigned users now see the normal product price for it.",
                parse_mode="HTML", reply_markup=_custom_rate_menu_markup(),
            )
            return
        try:
            price = Decimal(raw_value)
            if price <= 0:
                raise InvalidOperation
            price = price.quantize(Decimal("0.00000001"))
        except (InvalidOperation, ValueError):
            await answer_card(message, "❌ Send a valid price greater than 0, or <code>remove</code>.", parse_mode="HTML")
            return
        if control:
            control.price = price
            control.is_active = True
        else:
            db.add(ProductRateControl(product_id=product_id, price=price, is_active=True))
        db.commit()
    finally:
        db.close()
    invalidate_catalog_custom_prices_cache()
    await state.clear()
    await answer_card(message,
        f"✅ Rate Control saved\n\nProduct: <b>{safe(product_name)}</b>\nPrice: <b>${price}</b> per unit\n\n"
        "This price applies immediately to every user assigned to All Products (Rate Control).",
        parse_mode="HTML", reply_markup=_custom_rate_menu_markup(),
    )


@router.callback_query(F.data == "custom_rate_add")
async def custom_rate_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await state.set_state(CustomRateState.waiting_user_id)
    await _safe_edit_text(
        callback.message,
        "🏷 <b>SET CUSTOM RATE — STEP 1</b>\n\nSend the user's numeric Telegram ID.\n\n"
        "<i>Example: 7943742895</i>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="custom_rate_menu")]
        ]),
    )
    await callback.answer()


@router.message(CustomRateState.waiting_user_id)
async def custom_rate_user_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    try:
        telegram_id = int((message.text or "").strip())
    except ValueError:
        await answer_card(message, "❌ Send only the user's numeric Telegram ID.")
        return
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            await answer_card(message, "❌ User not found. The user must first start the bot.")
            return
        user_name = user.full_name
    finally:
        db.close()
    await state.update_data(custom_rate_user_id=telegram_id, custom_rate_user_name=user_name)
    await answer_card(message,
        f"✅ User: <b>{safe(user_name)}</b>\n<code>{telegram_id}</code>\n\n"
        "🏷 <b>STEP 2</b> — Where should this rate apply?", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 All Products (Rate Control)", callback_data="custom_rate_scope_control")],
            [InlineKeyboardButton(text="💲 One Price for All Products", callback_data="custom_rate_scope_all")],
            [InlineKeyboardButton(text="📦 One Product", callback_data="custom_rate_scope_product")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="custom_rate_menu")],
        ]),
    )


async def _custom_rate_ask_price(callback: CallbackQuery, state: FSMContext, product_id: int | None):
    await state.update_data(custom_rate_product_id=product_id)
    await state.set_state(CustomRateState.waiting_price)
    scope = "all products" if product_id is None else f"product #{product_id}"
    await _safe_edit_text(
        callback.message,
        f"🏷 <b>STEP 3</b> — <b>{scope}</b> selected.\n\n"
        "Send the fixed price per unit in USDT.\n\n<i>Example: 0.024 or 5.50</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="custom_rate_menu")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "custom_rate_scope_all")
async def custom_rate_scope_all(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    await _custom_rate_ask_price(callback, state, None)


@router.callback_query(F.data == "custom_rate_scope_control")
async def custom_rate_scope_control(callback: CallbackQuery, state: FSMContext):
    """Assign all saved Rate Control product prices to the chosen user."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get("custom_rate_user_id")
    if not telegram_id:
        await state.clear()
        await callback.answer("Setup expired. Start again.", show_alert=True)
        return
    db = SessionLocal()
    try:
        controlled_count = db.query(ProductRateControl).filter(ProductRateControl.is_active == True).count()
        if not controlled_count:
            await callback.answer("Set at least one product price in Rate Control first.", show_alert=True)
            return
        # Selecting Rate Control replaces the user's old manual all-products rule.
        for rate in db.query(CustomRate).filter(
                CustomRate.telegram_id == telegram_id,
                CustomRate.product_id.is_(None),
                CustomRate.is_active == True,
        ).all():
            rate.is_active = False
            rate.stopped_at = datetime.utcnow()
        for assignment in db.query(RateControlAssignment).filter(
                RateControlAssignment.telegram_id == telegram_id,
                RateControlAssignment.is_active == True,
        ).all():
            assignment.is_active = False
            assignment.stopped_at = datetime.utcnow()
        db.add(RateControlAssignment(telegram_id=telegram_id, is_active=True))
        db.commit()
    finally:
        db.close()
    invalidate_catalog_custom_prices_cache()
    await state.clear()
    await _safe_edit_text(
        callback.message,
        f"✅ <b>Rate Control active</b>\n\nUser: <code>{telegram_id}</code>\n"
        f"Products with saved controlled prices: <b>{controlled_count}</b>\n\n"
        "Those products use their Rate Control prices immediately. Products without a saved rate keep their normal price.",
        parse_mode="HTML", reply_markup=_custom_rate_menu_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "custom_rate_scope_product")
async def custom_rate_scope_product(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.is_active == True).order_by(Product.name.asc()).limit(80).all()
        rows = [[InlineKeyboardButton(
            text=f"#{product.id} {product.icon or '📦'} {product.name}"[:64],
            callback_data=f"custom_rate_product_{product.id}",
        )] for product in products]
    finally:
        db.close()
    if not rows:
        await callback.answer("No active products found.", show_alert=True)
        return
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="custom_rate_menu")])
    await _safe_edit_text(callback.message, "📦 <b>ONE PRODUCT SELECTED</b>\n\nChoose a product.", parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("custom_rate_product_"))
async def custom_rate_product(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        product_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Invalid product.", show_alert=True)
        return
    await _custom_rate_ask_price(callback, state, product_id)


@router.message(CustomRateState.waiting_price)
async def custom_rate_save(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return
    try:
        price = Decimal((message.text or "").strip())
        if price <= 0:
            raise InvalidOperation
        price = price.quantize(Decimal("0.00000001"))
    except (InvalidOperation, ValueError):
        await answer_card(message, "❌ Send a valid price greater than 0. Example: <code>0.024</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    telegram_id = data.get("custom_rate_user_id")
    product_id = data.get("custom_rate_product_id")
    if not telegram_id:
        await state.clear()
        await answer_card(message, "⚠️ Setup expired. Open Custom Rates and try again.")
        return
    db = SessionLocal()
    try:
        # A manual all-products price replaces the user's Rate Control assignment.
        if product_id is None:
            for assignment in db.query(RateControlAssignment).filter(
                    RateControlAssignment.telegram_id == telegram_id,
                    RateControlAssignment.is_active == True,
            ).all():
                assignment.is_active = False
                assignment.stopped_at = datetime.utcnow()
        existing = db.query(CustomRate).filter(CustomRate.telegram_id == telegram_id, CustomRate.is_active == True)
        existing = existing.filter(CustomRate.product_id.is_(None)) if product_id is None else existing.filter(
            CustomRate.product_id == product_id)
        for old_rate in existing.all():
            old_rate.is_active = False
            old_rate.stopped_at = datetime.utcnow()
        db.add(CustomRate(telegram_id=telegram_id, product_id=product_id, price=price, is_active=True))
        db.commit()
    finally:
        db.close()
    invalidate_catalog_custom_prices_cache()
    await state.clear()
    scope = "all products" if product_id is None else f"product #{product_id}"
    await answer_card(message,
        f"✅ <b>Custom rate active</b>\n\nUser: <code>{telegram_id}</code>\n"
        f"Scope: <b>{scope}</b>\nPrice: <b>${price}</b> per unit\n\n"
        "The next checkout uses this price immediately.", parse_mode="HTML",
        reply_markup=_custom_rate_menu_markup(),
    )


@router.callback_query(F.data == "custom_rate_list")
async def custom_rate_list(callback: CallbackQuery, alert_text: str | None = None):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    db = SessionLocal()
    try:
        rates = db.query(CustomRate).filter(CustomRate.is_active == True).order_by(CustomRate.id.desc()).limit(50).all()
        assignments = db.query(RateControlAssignment).filter(
            RateControlAssignment.is_active == True
        ).order_by(RateControlAssignment.id.desc()).limit(50).all()
        user_ids = {rate.telegram_id for rate in rates} | {item.telegram_id for item in assignments}
        product_ids = [rate.product_id for rate in rates if rate.product_id is not None]
        users = {u.telegram_id: u.full_name for u in
                 db.query(User).filter(User.telegram_id.in_(user_ids)).all()} if user_ids else {}
        products = {p.id: p.name for p in
                    db.query(Product).filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    finally:
        db.close()
    if not rates and not assignments:
        await _safe_edit_text(callback.message, "🏷 <b>ACTIVE CUSTOM RATES</b>\n\nNo active rates.", parse_mode="HTML",
                              reply_markup=_custom_rate_menu_markup())
        await callback.answer(alert_text, show_alert=bool(alert_text))
        return
    rows = []
    for assignment in assignments:
        user_label = users.get(assignment.telegram_id, str(assignment.telegram_id))
        rows.append([InlineKeyboardButton(
            text=f"🛑 {user_label} | All Products (Rate Control)"[:64],
            callback_data=f"rate_control_assign_stop_{assignment.id}",
        )])
    for rate in rates:
        user_label = users.get(rate.telegram_id, str(rate.telegram_id))
        scope = "All products" if rate.product_id is None else products.get(rate.product_id,
                                                                            f"Product #{rate.product_id}")
        rows.append([InlineKeyboardButton(text=f"🛑 {user_label} | {scope} | ${rate.price}"[:64],
                                          callback_data=f"custom_rate_stop_{rate.id}")])
    rows.append([InlineKeyboardButton(text="⬅ Back", callback_data="custom_rate_menu")])
    await _safe_edit_text(callback.message, "🏷 <b>ACTIVE CUSTOM RATES</b>\n\nTap a rate to stop it immediately.",
                          parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer(alert_text, show_alert=bool(alert_text))


@router.callback_query(F.data.startswith("rate_control_assign_stop_"))
async def rate_control_assignment_stop(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        assignment_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Invalid rate.", show_alert=True)
        return
    db = SessionLocal()
    try:
        assignment = db.get(RateControlAssignment, assignment_id)
        if not assignment or not assignment.is_active:
            await callback.answer("This rate is already stopped.", show_alert=True)
            return
        assignment.is_active = False
        assignment.stopped_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    invalidate_catalog_custom_prices_cache()
    await custom_rate_list(callback, "🛑 Rate Control stopped. Normal prices are active now.")


@router.callback_query(F.data.startswith("custom_rate_stop_"))
async def custom_rate_stop(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        rate_id = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("Invalid rate.", show_alert=True)
        return
    db = SessionLocal()
    try:
        rate = db.get(CustomRate, rate_id)
        if not rate or not rate.is_active:
            await callback.answer("This rate is already stopped.", show_alert=True)
            return
        rate.is_active = False
        rate.stopped_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    invalidate_catalog_custom_prices_cache()
    await custom_rate_list(callback, "🛑 Custom rate stopped. Normal price is active now.")
