# handlers/orders.py

import asyncio
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from html import escape as _esc
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import func, and_

from database import SessionLocal
from models.order import Order
from models.order_rating import OrderRating
from models.product import Product
from models.order_requirement_submission import OrderRequirementSubmission
from states.product_states import OrderRequirementStates
import config
from utils.ui import show, update_card, answer_card

router = Router()
logger = logging.getLogger(__name__)
SPENDS_PER_PAGE = 5

# ╔══════════════════════════════════════════════════════════════╗
# ║                     FSM STATES                              ║
# ╚══════════════════════════════════════════════════════════════╝

class OrderFilterStates(StatesGroup):
    waiting_filter = State()
    waiting_date_range = State()
    waiting_export_format = State()
    waiting_rating = State()
    waiting_reminder_time = State()


# ╔══════════════════════════════════════════════════════════════╗
# ║                  ORDER SUPPORT STATE                       ║
# ╚══════════════════════════════════════════════════════════════╝

_order_watchers: dict[int, set] = {}
_watchers_lock = asyncio.Lock()


def _load_order_rating(order_id: int, telegram_id: int) -> int | None:
    db = SessionLocal()
    try:
        rating = (
            db.query(OrderRating.rating)
            .filter(
                OrderRating.order_id == order_id,
                OrderRating.telegram_id == telegram_id,
            )
            .scalar()
        )
        return int(rating) if rating is not None else None
    finally:
        db.close()


def _save_order_rating(order_id: int, telegram_id: int, rating: int) -> bool:
    db = SessionLocal()
    try:
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.telegram_id == telegram_id)
            .with_for_update()
            .first()
        )
        if not order or order.status != "completed":
            return False

        saved_rating = db.query(OrderRating).filter(OrderRating.order_id == order_id).first()
        if saved_rating:
            saved_rating.rating = rating
        else:
            db.add(OrderRating(order_id=order_id, telegram_id=telegram_id, rating=rating))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _order_is_rateable(order_id: int, telegram_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.query(Order.id).filter(
            Order.id == order_id,
            Order.telegram_id == telegram_id,
            Order.status == "completed",
        ).first() is not None
    finally:
        db.close()


# ╔══════════════════════════════════════════════════════════════╗
# ║              STATUS CONFIGURATION                           ║
# ╚══════════════════════════════════════════════════════════════╝

STATUS_CONFIG = {
    "completed": {
        "emoji": "✅",
        "label": "Completed",
        "icon": "🟢",
        "style": "success",
        "progress": 100,
        "description": "Delivered successfully"
    },
    "pending_manual": {
        "emoji": "⏳",
        "label": "Pending Delivery",
        "icon": "🟡",
        "style": "primary",
        "progress": 50,
        "description": "Awaiting manual delivery"
    },
    "preorder": {
        "emoji": "📦",
        "label": "Pre-ordered",
        "icon": "🔵",
        "style": "primary",
        "progress": 25,
        "description": "Waiting for restock"
    },
    "refunded": {
        "emoji": "↩️",
        "label": "Refunded",
        "icon": "🟠",
        "style": "danger",
        "progress": 100,
        "description": "Amount refunded"
    },
    "deleted": {
        "emoji": "🗑",
        "label": "Deleted",
        "icon": "⚫",
        "style": "danger",
        "progress": 0,
        "description": "Order removed"
    },
    "failed": {
        "emoji": "❌",
        "label": "Failed",
        "icon": "🔴",
        "style": "danger",
        "progress": 0,
        "description": "Transaction failed"
    },
    "pending": {
        "emoji": "🔄",
        "label": "Pending",
        "icon": "🔵",
        "style": "primary",
        "progress": 30,
        "description": "Processing payment"
    },
}

DELIVERY_ETA = {
    "automatic": "Instant",
    "manual": "24 hours",
    "hybrid": "1-12 hours",
}


# ╔══════════════════════════════════════════════════════════════╗
# ║              UI HELPERS                                     ║
# ╚══════════════════════════════════════════════════════════════╝

def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _divider(char: str = "━", length: int = 35) -> str:
    return char * length


def _border_box(title: str, emoji: str = "📦") -> str:
    return (
        f"╔{'═' * 33}╗\n"
        f"║  {emoji} {title:<28}║\n"
        f"╚{'═' * 33}╝"
    )


def _progress_bar(percentage: int, length: int = 10) -> str:
    """Creates a visual progress bar."""
    filled = int(length * percentage / 100)
    empty = length - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percentage}%"


def _format_timestamp(dt: datetime, relative: bool = False) -> str:
    """Format timestamp with relative time option."""
    if relative:
        now = datetime.utcnow()
        diff = now - dt
        if diff < timedelta(minutes=1):
            return "Just now"
        elif diff < timedelta(hours=1):
            mins = int(diff.total_seconds() / 60)
            return f"{mins} min ago"
        elif diff < timedelta(days=1):
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}h ago"
        elif diff < timedelta(days=7):
            days = diff.days
            return f"{days}d ago"
    return dt.strftime("%d %b %Y, %I:%M %p")


def _fetch_product(product_id: int):
    """Fetch a product by ID."""
    db = SessionLocal()
    try:
        return db.query(Product).filter(Product.id == product_id).first()
    finally:
        db.close()


def _format_delivered_account(acc_str: str, index: int = 1) -> str:
    """
    Parses and formats multi-part credential strings cleanly.
    Handles 'email:password:link', 'email:password', or raw credential strings.
    """
    raw = acc_str.strip()
    if not raw:
        return ""

    if ":" in raw:
        parts = raw.split(":", 2)
        if len(parts) == 3 and ("http://" in parts[2] or "https://" in parts[2]):
            email = parts[0].strip()
            password = parts[1].strip()
            link = parts[2].strip()
            return (
                f"  <b>Item #{index}:</b>\n"
                f"  📧 <b>Email:</b> <code>{_esc(email)}</code>\n"
                f"  🔑 <b>Password:</b> <code>{_esc(password)}</code>\n"
                f"  🔗 <b>Mail Reader Link:</b> <code>{_esc(link)}</code>\n"
                f"  📋 <b>Full Combo:</b> <code>{_esc(raw)}</code>"
            )
        elif len(parts) == 2:
            email = parts[0].strip()
            password = parts[1].strip()
            return (
                f"  <b>Item #{index}:</b>\n"
                f"  📧 <b>Email:</b> <code>{_esc(email)}</code>\n"
                f"  🔑 <b>Password:</b> <code>{_esc(password)}</code>\n"
                f"  📋 <b>Full Combo:</b> <code>{_esc(raw)}</code>"
            )

    return f"  {index}. <code>{_esc(raw)}</code>"


# ╔══════════════════════════════════════════════════════════════╗
# ║              SUPPORT REDIRECT HANDLER                       ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "support_menu")
async def support_menu_redirect(callback: CallbackQuery):
    """Redirect to the actual support ticket system."""
    await callback.answer()

    text = (
        f"🎫 <b>SUPPORT CENTER</b>\n\n"
        f"👋 <b>Need help with your order?</b>\n\n"
        f"{_divider('─')}\n\n"
        f"📋 <b>Options:</b>\n\n"
        f"  🎫 <b>Create Ticket</b> — Report an issue\n"
        f"  📋 <b>View Tickets</b> — Track existing\n"
        f"  🔍 <b>Track by ID</b> — Find a ticket\n"
        f"  ❓ <b>FAQ</b> — Common answers\n\n"
        f"{_divider('─')}\n\n"
        f"💡 <i>Our team responds within 2 hours!</i>"
    )

    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎫 Open Support Center",
                    callback_data="support_ticket",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Back to Orders",
                    callback_data="orders_menu",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="🏠 Main Menu",
                    callback_data="main_menu",
                    style="primary"
                )
            ]
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)


# ╔══════════════════════════════════════════════════════════════╗
# ║              FORMATTING FUNCTIONS                           ║
# ╚══════════════════════════════════════════════════════════════╝

def _format_order(order: Order, detailed: bool = True, rating: int | None = None) -> str:
    """Format a single order with beautiful, color-coded output."""

    status_config = STATUS_CONFIG.get(
        order.status,
        {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary", "progress": 50, "description": "Unknown"}
    )

    # Progress bar
    progress_bar = _progress_bar(status_config.get("progress", 50))

    # Header
    header = (
        f"{status_config['icon']} <b>Order #{order.id}</b>\n"
        f"   └ {status_config['emoji']} <b>{status_config['label']}</b>\n"
        f"   └ Progress: {progress_bar}\n"
    )

    # Product details
    product_name = order.product_name if order.product_name else "Unknown Product"
    body = (
        f"\n📦 <b>Product:</b> {product_name}\n"
        f"🔢 <b>Quantity:</b> {order.quantity if order.quantity else 1}x\n"
        f"💰 <b>Amount:</b> ${float(order.amount):.2f}\n"
    )

    # Order type & ETA
    if order.is_preorder:
        body += f"🏷 <b>Type:</b> 📦 Preorder\n"
        body += f"⏱ <b>ETA:</b> When restocked\n"
    elif order.delivery_type:
        delivery_labels = {
            "automatic": "🤖 Auto-Delivery",
            "manual": "👨‍💼 Manual Delivery",
            "hybrid": "🔀 Hybrid",
        }
        delivery_label = delivery_labels.get(order.delivery_type, order.delivery_type)
        eta = DELIVERY_ETA.get(order.delivery_type, "Unknown")
        body += f"🏷 <b>Type:</b> {delivery_label}\n"
        body += f"⏱ <b>ETA:</b> {eta}\n"

    # Delivered accounts (if any and detailed mode)
    if detailed and getattr(order, "delivered_account", None) and order.delivered_account.strip():
        accounts = [a.strip() for a in order.delivered_account.strip().split("\n") if a.strip()]
        body += f"\n🔑 <b>Delivered Credentials:</b>\n\n"
        formatted_list = [_format_delivered_account(acc, i) for i, acc in enumerate(accounts[:50], 1)]
        body += "\n\n".join(formatted_list) + "\n"
        if len(accounts) > 50:
            body += f"\n   <i>...and {len(accounts) - 50} more</i>\n"

    # Dates
    if getattr(order, "created_at", None):
        body += f"\n📅 <b>Ordered:</b> {_format_timestamp(order.created_at)}\n"

    if order.status == "refunded" and getattr(order, "refunded_at", None):
        body += f"↩️ <b>Refunded:</b> {_format_timestamp(order.refunded_at)}\n"

    # Rating if exists
    if rating:
        stars = "⭐" * rating + "☆" * (5 - rating)
        body += f"\n🌟 <b>Your Rating:</b> {stars}\n"

    return header + body


def _format_orders_summary(orders: list) -> str:
    """Format multiple orders with a beautiful summary and analytics."""

    # Count statistics
    total_orders = len(orders)
    completed = sum(1 for o in orders if o.status == "completed")
    pending = sum(1 for o in orders if o.status in ["pending_manual", "preorder", "pending"])
    refunded = sum(1 for o in orders if o.status == "refunded")
    total_spent = sum(float(o.amount) for o in orders if o.status not in ["refunded", "deleted"])
    avg_order = total_spent / max(total_orders, 1)

    # Most purchased products
    product_counts = {}
    for o in orders:
        name = o.product_name or "Unknown"
        product_counts[name] = product_counts.get(name, 0) + 1
    top_products = sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # Header with statistics
    header = (
        f"📦 <b>YOUR ORDERS</b>\n\n"
        f"{_divider('═')}\n"
        f"📊 <b>ANALYTICS DASHBOARD</b>\n"
        f"{_divider('═')}\n\n"
        f"📦 Total Orders: <b>{total_orders}</b>\n"
        f"✅ Completed: <b>{completed}</b>\n"
        f"⏳ Pending: <b>{pending}</b>\n"
        f"↩️ Refunded: <b>{refunded}</b>\n"
        f"💰 Total Spent: <b>${total_spent:.2f}</b>\n"
        f"📊 Avg Order: <b>${avg_order:.2f}</b>\n\n"
    )

    if top_products:
        header += f"{_divider('─')}\n"
        header += f"🏆 <b>Top Products:</b>\n"
        for i, (name, count) in enumerate(top_products, 1):
            medal = ["🥇", "🥈", "🥉"][i - 1]
            header += f"   {medal} {name} — {count}x\n"

    header += f"\n{_divider('═')}\n\n"

    # Individual orders (compact)
    orders_text = ""
    for order in orders[:15]:  # Show max 15 in compact view
        status_config = STATUS_CONFIG.get(
            order.status,
            {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary"}
        )

        orders_text += (
            f"{status_config['icon']} <b>#{order.id}</b> — "
            f"{order.product_name or 'Unknown'}\n"
            f"   └ {status_config['emoji']} {status_config['label']} | "
            f"${float(order.amount):.2f}"
        )

        if getattr(order, "created_at", None):
            orders_text += f" | {_format_timestamp(order.created_at, relative=True)}"

        orders_text += "\n"

    if len(orders) > 15:
        orders_text += f"\n<i>...and {len(orders) - 15} more orders</i>\n"

    return header + orders_text + f"\n{_divider('═')}"


# ╔══════════════════════════════════════════════════════════════╗
# ║              MAIN ORDERS MENU                               ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "orders_menu")
async def my_orders(callback: CallbackQuery):
    """Display user's orders with beautiful formatting, analytics, and color coding."""
    await callback.answer()

    db = SessionLocal()
    try:
        user_id = callback.from_user.id

        orders = (
            db.query(Order)
            .filter(Order.telegram_id == user_id)
            .order_by(Order.id.desc())
            .all()
        )
    finally:
        db.close()

    # No orders
    if not orders:
        await show(
            callback,
            "📭 <b>No recent orders found.</b>",
            parse_mode="HTML",
        )
        return

    text = "📦 <b>YOUR ORDERS</b>\nTap a button below to view order details."

    # Create detailed order buttons
    recent_orders = orders[:10]

    keyboard_buttons = []
    for order in recent_orders:
        status_config = STATUS_CONFIG.get(
            order.status,
            {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary"}
        )

        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"{status_config['icon']} #{order.id} — {order.product_name or 'Unknown'} (${float(order.amount):.2f})",
                callback_data=f"order_detail_{order.id}",
                style=status_config["style"]
            )
        ])

    # Advanced feature buttons
    feature_buttons = []

    # Filter button
    feature_buttons.append(
        InlineKeyboardButton(
            text="🔍 Filter",
            callback_data="order_filter_menu",
            style="primary"
        )
    )

    # Export button
    feature_buttons.append(
        InlineKeyboardButton(
            text="📤 Export",
            callback_data="order_export_menu",
            style="primary"
        )
    )

    # Analytics button
    if len(orders) > 3:
        feature_buttons.append(
            InlineKeyboardButton(
                text="📊 Analytics",
                callback_data="order_analytics",
                style="primary"
            )
        )

    # Show all button
    if len(orders) > 10:
        feature_buttons.append(
            InlineKeyboardButton(
                text=f"📋 All ({len(orders)})",
                callback_data="orders_view_all",
                style="primary"
            )
        )

    # Arrange feature buttons in rows of 2
    feature_rows = []
    for i in range(0, len(feature_buttons), 2):
        row = feature_buttons[i:i + 2]
        feature_rows.append(row)

    keyboard_buttons.extend(feature_rows)

    # Support button
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="🆘 Support",
            callback_data="support_menu",
            style="danger"
        )
    ])

    # Navigation
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="🔄 Refresh",
            callback_data="orders_menu",
            style="primary"
        ),
        InlineKeyboardButton(
            text="⬅ Main Menu",
            callback_data="main_menu",
            style="primary"
        )
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await show(callback, text, parse_mode="HTML", reply_markup=markup)


# ╔══════════════════════════════════════════════════════════════╗
# ║              ORDER DETAIL VIEW                              ║
# ╚══════════════════════════════════════════════════════════════╝

def _order_requirement_context(order_id: int, telegram_id: int):
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id, Order.telegram_id == telegram_id).first()
        if not order:
            return None, [], None
        product = db.query(Product).filter(Product.id == order.product_id).first() if order.product_id else None
        submission = db.query(OrderRequirementSubmission).filter_by(order_id=order_id).first()
        requirements = product.required_info if product and isinstance(product.required_info, list) else []
        return order, requirements, submission
    finally:
        db.close()


def _requirement_prompt(field: dict, index: int, total: int) -> str:
    kind = str(field.get("type", "text")).lower()
    label = _esc(str(field.get("label") or kind.title()))
    hint = {
        "email": "Send the email address as text.",
        "phone": "Send the phone number as text.",
        "photo": "Upload a clear photo or screenshot.",
        "contact": "Use Telegram's paperclip menu to share your contact.",
        "text": "Send the requested information as text.",
    }.get(kind, "Send the requested information.")
    return f"🧾 <b>Order information {index + 1}/{total}</b>\n\n<b>{label}</b>\n{hint}\n\nSend /cancel to stop; you can resume from My Orders."


def _save_order_requirement_submission(order_id: int, telegram_id: int, values: dict) -> dict:
    db = SessionLocal()
    try:
        order = db.query(Order).filter(
            Order.id == order_id, Order.telegram_id == telegram_id
        ).with_for_update().first()
        if not order:
            return {"error": "Order not found."}
        existing = db.query(OrderRequirementSubmission).filter_by(order_id=order_id).first()
        if existing:
            return {"error": "Information was already submitted for this order."}
        if order.status != "pending_info":
            return {"error": "This order is not waiting for buyer information."}
        db.add(OrderRequirementSubmission(order_id=order.id, telegram_id=telegram_id, values=values))
        if order.is_preorder:
            order.status = "preorder"
        elif order.reserved_delivery:
            order.delivered_account = order.reserved_delivery
            order.reserved_delivery = None
            order.status = "completed"
        else:
            order.status = "pending_manual"
        db.commit()
        return {"status": order.status, "delivery": order.delivered_account, "product_name": order.product_name}
    except Exception:
        db.rollback()
        logger.exception("Could not save order requirements for order %s", order_id)
        return {"error": "Could not save the information. Please try again."}
    finally:
        db.close()


@router.callback_query(F.data.startswith("order_submit_info_"))
async def order_submit_info(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.removeprefix("order_submit_info_"))
    order, requirements, submission = await asyncio.to_thread(
        _order_requirement_context, order_id, callback.from_user.id
    )
    if not order or order.status != "pending_info" or not requirements or submission:
        await callback.answer("This order cannot accept more information.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(order_requirement_order_id=order_id, order_requirement_fields=requirements,
                            order_requirement_index=0, order_requirement_values={})
    await state.set_state(OrderRequirementStates.waiting_input)
    await show(callback, _requirement_prompt(requirements[0], 0, len(requirements)), parse_mode="HTML",
               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="order_cancel_info")]]))


@router.callback_query(F.data == "order_cancel_info")
async def order_cancel_info(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Stopped. You can continue from My Orders.")
    await show(callback, "Information entry stopped. Open the order again when you're ready.",
               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📦 My Orders", callback_data="orders_menu")]]))


@router.message(OrderRequirementStates.waiting_input)
async def order_requirement_input(message: Message, state: FSMContext):
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await answer_card(message, "Stopped. You can continue from My Orders.")
        return
    data = await state.get_data()
    fields = data.get("order_requirement_fields") or []
    index = int(data.get("order_requirement_index", 0))
    if index >= len(fields):
        await state.clear()
        await answer_card(message, "This information request expired. Open the order again.")
        return
    field = fields[index]
    kind = str(field.get("type", "text")).lower()
    if kind == "photo":
        if not message.photo:
            await answer_card(message, "Please upload a photo for this field.")
            return
        value = {"kind": "photo", "file_id": message.photo[-1].file_id}
    elif kind == "contact":
        if not message.contact:
            await answer_card(message, "Please share a Telegram contact using the attachment menu.")
            return
        value = {"kind": "contact", "phone": message.contact.phone_number,
                 "first_name": message.contact.first_name, "user_id": message.contact.user_id}
    elif kind == "email":
        text = (message.text or "").strip()
        if len(text) > 254 or "@" not in text or "." not in text.rsplit("@", 1)[-1]:
            await answer_card(message, "Please send a valid email address.")
            return
        value = {"kind": "text", "value": text[:254]}
    elif kind == "phone":
        text = (message.text or "").strip()
        digits = sum(ch.isdigit() for ch in text)
        if digits < 7 or len(text) > 40:
            await answer_card(message, "Please send a valid phone/contact number.")
            return
        value = {"kind": "text", "value": text}
    else:
        text = (message.text or "").strip()
        if not text or len(text) > 3000:
            await answer_card(message, "Please send text up to 3000 characters.")
            return
        value = {"kind": "text", "value": text}

    values = data.get("order_requirement_values") or {}
    values[str(field.get("label") or kind)] = value
    index += 1
    if index < len(fields):
        await state.update_data(order_requirement_values=values, order_requirement_index=index)
        await answer_card(message, _requirement_prompt(fields[index], index, len(fields)), parse_mode="HTML")
        return

    order_id = int(data["order_requirement_order_id"])
    result = await asyncio.to_thread(_save_order_requirement_submission, order_id, message.from_user.id, values)
    await state.clear()
    if result.get("error"):
        await answer_card(message, f"⚠️ {_esc(result['error'])}")
        return
    if result["status"] == "completed":
        delivery = result.get("delivery") or ""
        await answer_card(message,
            f"✅ <b>Information received — Order #{order_id} is delivered.</b>\n\n"
            f"<code>{_esc(delivery)}</code>" if delivery else f"✅ Order #{order_id} is complete.",
            parse_mode="HTML")
    elif result["status"] == "preorder":
        await answer_card(message, f"✅ Information saved for Order #{order_id}. It will be fulfilled when stock is available.")
    else:
        await answer_card(message, f"✅ Information saved for Order #{order_id}. The team can now fulfill it.")
        channel = getattr(config, "ADMIN_ORDER_NOTIFICATION_CHANNEL_ID", "")
        if channel:
            try:
                await message.bot.send_message(channel,
                    f"🧾 <b>Buyer information received</b>\nOrder: <code>#{order_id}</code>\n"
                    f"Buyer: <code>{message.from_user.id}</code>\nOpen Admin → Orders to review.", parse_mode="HTML")
            except Exception:
                logger.exception("Failed to notify admins of order information submission")

@router.callback_query(F.data.startswith("order_detail_"))
async def order_detail(callback: CallbackQuery):
    """Show detailed view of a single order with advanced features."""
    await callback.answer()

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        user_id = callback.from_user.id

        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.telegram_id == user_id
            )
            .first()
        )
    finally:
        db.close()

    if not order:
        await show(
            callback,
            "❌ <b>Order Not Found</b>\n\nThis order doesn't belong to you or was deleted.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="⬅ Back to Orders",
                        callback_data="orders_menu",
                        style="primary"
                    )
                ]]
            )
        )
        return

    existing_rating = await asyncio.to_thread(
        _load_order_rating, order.id, callback.from_user.id
    )
    text = _format_order(order, detailed=True, rating=existing_rating)

    # Status-specific messages
    text += f"\n{_divider('═')}\n"

    status_config = STATUS_CONFIG.get(
        order.status,
        {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary", "progress": 50, "description": "Unknown"}
    )

    status_messages = {
        "pending_manual": (
            "⏳ <b>Manual Delivery in Progress</b>\n\n"
            "👨‍💼 Our team is preparing your order.\n"
            f"⏱ Estimated delivery: {DELIVERY_ETA.get(order.delivery_type, '24 hours')}\n\n"
            "<i>You'll be notified when it's ready! 🔔</i>"
        ),
        "preorder": (
            "📦 <b>Preorder Status</b>\n\n"
            "🔄 We're restocking this product.\n"
            "🔔 You'll receive a notification as soon as\n"
            "   stock becomes available.\n\n"
            "<i>Thank you for your patience! 🙏</i>"
        ),
        "completed": (
            "✅ <b>Order Completed!</b>\n\n"
            "🎉 Enjoy your purchase!\n"
            "💡 Tip: Rate this order to help us improve.\n\n"
            "<i>Thank you for shopping with us! 🙏</i>"
        ),
        "refunded": (
            "↩️ <b>Order Refunded</b>\n\n"
            "💰 The amount has been credited back to your balance.\n"
            "📋 Contact support if you have any questions."
        ),
        "deleted": (
            "🗑 <b>Order Deleted</b>\n\n"
            "This order has been removed from the system."
        ),
        "failed": (
            "❌ <b>Order Failed</b>\n\n"
            "The transaction was not completed.\n"
            "💰 No funds were deducted.\n\n"
            "🔄 Please try again or contact support."
        ),
        "pending": (
            "🔄 <b>Payment Processing</b>\n\n"
            "⏳ Your payment is being processed.\n"
            "This usually takes a few minutes.\n\n"
            "<i>Do not close this window.</i>"
        ),
        "pending_info": (
            "🧾 <b>Waiting for your information</b>\n\n"
            "Open the button below and submit the details requested for this product. "
            "Delivery is held until the details are received."
        ),
    }

    text += f"\n{status_messages.get(order.status, '')}"

    # Check if product has delivery instructions
    has_instruction = False
    product_id = getattr(order, "product_id", None)
    if product_id:
        product = _fetch_product(product_id)
        if product and product.delivery_instruction:
            has_instruction = True

    requirement_order, required_fields, requirement_submission = await asyncio.to_thread(
        _order_requirement_context, order.id, callback.from_user.id
    )

    # Action buttons based on status
    action_buttons = []

    if order.status == "pending_info" and required_fields and not requirement_submission:
        action_buttons.append([InlineKeyboardButton(
            text="🧾 Send Required Information",
            callback_data=f"order_submit_info_{order.id}",
            style="success",
        )])
    elif requirement_submission:
        action_buttons.append([InlineKeyboardButton(
            text="✅ Information Submitted",
            callback_data=f"order_detail_{order.id}",
            style="success",
        )])

    # Delivery Instructions button — shows for completed orders that have instructions
    if order.status == "completed" and has_instruction:
        action_buttons.append([
            InlineKeyboardButton(
                text="📋 📖 Delivery Instructions",
                callback_data=f"delivery_instruction_{product_id}",
                style="primary"
            )
        ])

    # Support button for active issues
    if order.status in ["pending_manual", "preorder", "pending"]:
        action_buttons.append([
            InlineKeyboardButton(
                text="🆘 Get Support",
                callback_data="support_menu",
                style="danger"
            ),
            InlineKeyboardButton(
                text="📎 Get Receipt",
                callback_data=f"order_receipt_{order.id}",
                style="primary"
            )
        ])

    # Rating for completed orders
    elif order.status == "completed":
        if not existing_rating:
            action_buttons.append([
                InlineKeyboardButton(
                    text="⭐ Rate This Order",
                    callback_data=f"order_rate_{order.id}",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="📎 Receipt",
                    callback_data=f"order_receipt_{order.id}",
                    style="primary"
                )
            ])
        else:
            stars = "⭐" * existing_rating + "☆" * (5 - existing_rating)
            action_buttons.append([
                InlineKeyboardButton(
                    text=f"🌟 Rated: {stars}",
                    callback_data=f"order_rate_{order.id}",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="📎 Receipt",
                    callback_data=f"order_receipt_{order.id}",
                    style="primary"
                )
            ])
        if product_id:
            action_buttons.append([
                InlineKeyboardButton(
                    text="🔁 Buy Again",
                    callback_data=f"reorder_{order.id}",
                    style="success",
                )
            ])

    # Share button
    action_buttons.append([
        InlineKeyboardButton(
            text="🔗 Share Order",
            callback_data=f"order_share_{order.id}",
            style="primary"
        ),
        InlineKeyboardButton(
            text="🔄 Refresh Status",
            callback_data=f"order_detail_{order.id}",
            style="primary"
        )
    ])

    # Navigation
    action_buttons.append([
        InlineKeyboardButton(
            text="📋 All Orders",
            callback_data="orders_menu",
            style="primary"
        ),
        InlineKeyboardButton(
            text="🏠 Main Menu",
            callback_data="main_menu",
            style="primary"
        )
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=action_buttons)

    await show(callback, text, parse_mode="HTML", reply_markup=markup)


def _load_user_spends(telegram_id: int, page: int):
    db = SessionLocal()
    try:
        query = db.query(Order).filter(Order.telegram_id == telegram_id).order_by(Order.id.desc())
        total = query.count()
        orders = query.offset(page * SPENDS_PER_PAGE).limit(SPENDS_PER_PAGE).all()
        return orders, total
    finally:
        db.close()


@router.callback_query(F.data == "my_spends")
async def my_spends(callback: CallbackQuery):
    await _show_spends_page(callback, 0)


@router.callback_query(F.data.startswith("spends_page_"))
async def spends_page(callback: CallbackQuery):
    try:
        page = max(0, int(callback.data.rsplit("_", 1)[-1]))
    except (ValueError, IndexError):
        page = 0
    await _show_spends_page(callback, page)


async def _show_spends_page(callback: CallbackQuery, page: int):
    await callback.answer()
    orders, total = await asyncio.to_thread(_load_user_spends, callback.from_user.id, page)
    pages = max(1, (total + SPENDS_PER_PAGE - 1) // SPENDS_PER_PAGE)
    page = min(page, pages - 1)
    if page and not orders:
        orders, total = await asyncio.to_thread(_load_user_spends, callback.from_user.id, page)

    lines = ["💸 <b>MY SPENDS</b>"]
    if not orders:
        lines.append("No purchases yet.")
    else:
        start = page * SPENDS_PER_PAGE + 1
        lines.append(f"Showing {start}–{start + len(orders) - 1} of {total} purchases\n")
        for order in orders:
            status = STATUS_CONFIG.get(order.status, {}).get("label", order.status or "Unknown")
            when = _format_timestamp(order.created_at, relative=True) if order.created_at else "Date unavailable"
            lines.append(
                f"🧾 <b>Order #{order.id}</b> — {_esc(order.product_name or 'Product')}\n"
                f"└ ${float(order.amount):.2f} | {_esc(status)} | {when}"
            )

    keyboard = []
    page_buttons = []
    if page > 0:
        page_buttons.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"spends_page_{page - 1}"))
    if page < pages - 1:
        page_buttons.append(InlineKeyboardButton(text="Next ➡", callback_data=f"spends_page_{page + 1}"))
    if page_buttons:
        keyboard.append(page_buttons)
    keyboard.extend([
        [InlineKeyboardButton(text="📥 My Deposits", callback_data="my_deposits")],
        [InlineKeyboardButton(text="💰 Wallet", callback_data="deposit_start")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
    ])
    await show(
        callback,
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )


@router.callback_query(F.data.startswith("reorder_"))
async def reorder_order(callback: CallbackQuery):
    try:
        order_id = int(callback.data.removeprefix("reorder_"))
    except (TypeError, ValueError):
        await callback.answer("Invalid order.", show_alert=True)
        return

    def _load_reorder_target():
        db = SessionLocal()
        try:
            order = db.query(Order).filter(
                Order.id == order_id,
                Order.telegram_id == callback.from_user.id,
                Order.status == "completed",
            ).first()
            if not order or not order.product_id:
                return None
            product = db.query(Product).filter(
                Product.id == order.product_id,
                Product.is_active == True,
            ).first()
            return product.id if product else None
        finally:
            db.close()

    product_id = await asyncio.to_thread(_load_reorder_target)
    if not product_id:
        await callback.answer()
        await show(
            callback,
            "This product is no longer available for reorder.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu")
            ]]),
        )
        return

    from handlers.products import product_info

    await product_info(callback, product_id)


# ╔══════════════════════════════════════════════════════════════╗
# ║         DELIVERY INSTRUCTION BUTTON HANDLER (ORDERS)       ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("delivery_instruction_"))
async def show_delivery_instruction_from_orders(callback: CallbackQuery):
    """Show delivery instruction when clicked from order detail view."""
    await callback.answer()
    product_id = int(callback.data.split("_")[2])
    product = _fetch_product(product_id)

    if not product or not product.delivery_instruction:
        await callback.answer("📋 No delivery instructions available.", show_alert=True)
        return

    text = (
        f"╔{'═' * 30}╗\n"
        f"║  📋 DELIVERY INSTRUCTIONS      ║\n"
        f"╚{'═' * 30}╝\n\n"
        f"<b>{product.icon or '📦'} {product.name}</b>\n\n"
        f"{'─' * 30}\n\n"
        f"<b>⚠️ IMPORTANT — READ CAREFULLY:</b>\n\n"
        f"<blockquote>{product.delivery_instruction}</blockquote>\n\n"
        f"{'═' * 30}\n\n"
        f"<i>💡 Please follow these instructions carefully\n"
        f"to ensure a smooth experience.</i>\n\n"
        f"<i>If you have any issues, contact support!</i>"
    )

    await answer_card(callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Back to Orders", callback_data="orders_menu", style="success"),
                 InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="primary")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              RECEIPT GENERATION                             ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("order_receipt_"))
async def order_receipt(callback: CallbackQuery):
    """Generate and send a downloadable receipt."""
    await callback.answer("📎 Generating receipt...")

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.telegram_id == callback.from_user.id)
            .first()
        )
    finally:
        db.close()

    if not order:
        await callback.answer("❌ Order not found.", show_alert=True)
        return

    status_config = STATUS_CONFIG.get(
        order.status,
        {"emoji": "❓", "label": order.status, "icon": "⚪"}
    )

    # Build receipt text
    receipt = (
        "╔══════════════════════════════════╗\n"
        "║        ORDER RECEIPT             ║\n"
        "╚══════════════════════════════════╝\n\n"
        f"Receipt #REC-{order.id}-{order.created_at.strftime('%Y%m%d') if order.created_at else 'N/A'}\n"
        f"{'─' * 36}\n\n"
        f"Order ID:      #{order.id}\n"
        f"Product:       {order.product_name or 'Unknown'}\n"
        f"Quantity:      {order.quantity or 1}x\n"
        f"Amount:        ${float(order.amount):.2f}\n"
        f"Status:        {status_config['emoji']} {status_config['label']}\n"
        f"Delivery:      {order.delivery_type or 'Standard'}\n"
        f"Preorder:      {'Yes' if order.is_preorder else 'No'}\n"
        f"Date:          {order.created_at.strftime('%d %b %Y, %I:%M %p UTC') if order.created_at else 'N/A'}\n"
        f"{'─' * 36}\n\n"
    )

    if order.status == "completed" and order.delivered_account:
        receipt += f"Delivered Accounts:\n{order.delivered_account}\n\n"

    receipt += (
        f"{'─' * 36}\n"
        f"Thank you for your purchase!\n"
        f"Generated: {datetime.utcnow().strftime('%d %b %Y, %I:%M %p UTC')}\n"
    )

    # Send as text message
    await show(
        callback,
        f"<pre>{receipt}</pre>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Back to Order",
                        callback_data=f"order_detail_{order.id}",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="📤 Export as CSV",
                        callback_data=f"order_export_single_{order.id}",
                        style="success"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              ORDER RATING SYSTEM                            ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("order_rate_"))
async def order_rate(callback: CallbackQuery):
    """Show rating options for a completed order."""
    order_id = int(callback.data.split("_")[2])
    if not await asyncio.to_thread(_order_is_rateable, order_id, callback.from_user.id):
        await callback.answer("Only your completed orders can be rated.", show_alert=True)
        return
    existing = await asyncio.to_thread(_load_order_rating, order_id, callback.from_user.id)
    await callback.answer()

    text = (
        f"⭐ <b>RATE YOUR ORDER</b>\n\n"
        f"📦 <b>Order #{order_id}</b>\n\n"
        f"{_divider('─')}\n\n"
        f"How was your experience?\n\n"
        f"Your feedback helps us improve! 🙏\n"
    )

    if existing:
        text += f"\n<i>Current rating: {'⭐' * existing}</i>\n"

    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="😡 1 — Terrible",
                    callback_data=f"rate_order_{order_id}_1",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😐 2 — Poor",
                    callback_data=f"rate_order_{order_id}_2",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😊 3 — Good",
                    callback_data=f"rate_order_{order_id}_3",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😄 4 — Great",
                    callback_data=f"rate_order_{order_id}_4",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌟 5 — Excellent",
                    callback_data=f"rate_order_{order_id}_5",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back to Order",
                    callback_data=f"order_detail_{order_id}",
                    style="primary"
                )
            ]
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)


@router.callback_query(F.data.startswith("rate_order_"))
async def handle_order_rating(callback: CallbackQuery):
    """Save the user's rating for an order."""
    parts = callback.data.split("_")
    order_id = int(parts[2])
    rating = int(parts[3])
    if rating not in range(1, 6):
        await callback.answer("Invalid rating.", show_alert=True)
        return
    try:
        saved = await asyncio.to_thread(
            _save_order_rating, order_id, callback.from_user.id, rating
        )
    except Exception:
        logger.exception("Failed to save rating for order %s", order_id)
        await callback.answer("Could not save your rating. Please try again.", show_alert=True)
        return
    if not saved:
        await callback.answer("Only your completed orders can be rated.", show_alert=True)
        return
    await callback.answer("Rating saved.")

    rating_labels = {1: "Terrible", 2: "Poor", 3: "Good", 4: "Great", 5: "Excellent"}
    rating_emoji = {1: "😡", 2: "😐", 3: "😊", 4: "😄", 5: "🌟"}

    stars = "⭐" * rating + "☆" * (5 - rating)

    await show(
        callback,
        (
            f"{rating_emoji[rating]} <b>RATING SAVED</b>\n\n"
            f"📦 <b>Order #{order_id}</b>\n\n"
            f"Your rating: {stars}\n"
            f"Rating: <b>{rating_labels[rating]}</b>\n\n"
            f"{_divider('─')}\n\n"
            f"Thank you for your feedback! 🙏\n\n"
            f"<i>This helps us serve you better.</i>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Back to Order",
                        callback_data=f"order_detail_{order_id}",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="📋 All Orders",
                        callback_data="orders_menu",
                        style="primary"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              ORDER SHARING                                  ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("order_share_"))
async def order_share(callback: CallbackQuery):
    """Generate a shareable order status card."""
    await callback.answer()

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        order = (
            db.query(Order)
            .filter(Order.id == order_id, Order.telegram_id == callback.from_user.id)
            .first()
        )
    finally:
        db.close()

    if not order:
        await callback.answer("❌ Order not found.", show_alert=True)
        return

    status_config = STATUS_CONFIG.get(
        order.status,
        {"emoji": "❓", "label": order.status, "icon": "⚪"}
    )

    share_text = (
        f"🛒 <b>Order #{order.id}</b>\n"
        f"{'─' * 25}\n"
        f"📦 {order.product_name or 'Product'}\n"
        f"💰 ${float(order.amount):.2f}\n"
        f"📊 {status_config['emoji']} {status_config['label']}\n"
        f"📅 {order.created_at.strftime('%d %b %Y') if order.created_at else 'N/A'}\n"
        f"{'─' * 25}\n"
        f"⚡ <i>Shared via our bot</i>"
    )

    await show(
        callback,
        (
            f"🔗 <b>SHARE ORDER</b>\n\n"
            f"📤 <b>Shareable Card:</b>\n\n"
            f"<blockquote>{share_text}</blockquote>\n\n"
            f"{_divider('─')}\n\n"
            f"💡 <b>How to share:</b>\n"
            f"• Copy the text above\n"
            f"• Forward to anyone!\n"
            f"• No sensitive data exposed 🔒\n\n"
            f"<i>Account details are hidden for security.</i>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Back to Order",
                        callback_data=f"order_detail_{order.id}",
                        style="primary"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              ORDER FILTER SYSTEM                            ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "order_filter_menu")
async def order_filter_menu(callback: CallbackQuery):
    """Show filter options."""
    await callback.answer()

    text = (
        f"🔍 <b>FILTER ORDERS</b>\n\n"
        f"📊 <b>Filter by:</b>\n\n"
        f"{_divider('─')}\n\n"
        f"  🟢 <b>Completed</b> — Only delivered\n"
        f"  🟡 <b>Pending</b> — Awaiting delivery\n"
        f"  🔵 <b>Preorders</b> — Waiting for restock\n"
        f"  🟠 <b>Refunded</b> — Amount returned\n\n"
        f"{_divider('─')}\n\n"
        f"Select a filter below:"
    )

    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Completed",
                    callback_data="filter_status_completed",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="⏳ Pending",
                    callback_data="filter_status_pending",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Preorders",
                    callback_data="filter_status_preorder",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="↩️ Refunded",
                    callback_data="filter_status_refunded",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Last 7 Days",
                    callback_data="filter_date_7days",
                    style="primary"
                ),
                InlineKeyboardButton(
                    text="📅 Last 30 Days",
                    callback_data="filter_date_30days",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Search by ID",
                    callback_data="order_search_by_id",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Clear Filters",
                    callback_data="orders_menu",
                    style="danger"
                ),
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="orders_menu",
                    style="primary"
                )
            ]
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)


@router.callback_query(F.data.startswith("filter_status_"))
@router.callback_query(F.data.startswith("filter_date_"))
async def apply_order_filter(callback: CallbackQuery):
    """Apply the selected filter."""
    await callback.answer()

    filter_type = callback.data

    db = SessionLocal()
    try:
        query = db.query(Order).filter(Order.telegram_id == callback.from_user.id)

        if filter_type.startswith("filter_status_"):
            status = filter_type.replace("filter_status_", "")
            if status == "pending":
                query = query.filter(Order.status.in_(["pending_manual", "preorder", "pending"]))
            else:
                query = query.filter(Order.status == status)
            filter_label = status.capitalize()

        elif filter_type == "filter_date_7days":
            cutoff = datetime.utcnow() - timedelta(days=7)
            query = query.filter(Order.created_at >= cutoff)
            filter_label = "Last 7 Days"

        elif filter_type == "filter_date_30days":
            cutoff = datetime.utcnow() - timedelta(days=30)
            query = query.filter(Order.created_at >= cutoff)
            filter_label = "Last 30 Days"

        else:
            filter_label = "All"

        orders = query.order_by(Order.id.desc()).all()
    finally:
        db.close()

    text = (
        f"🔍 <b>FILTERED RESULTS</b>\n\n"
        f"📊 <b>Filter:</b> {filter_label}\n"
        f"📦 <b>Found:</b> {len(orders)} order(s)\n\n"
        f"{_divider('═')}\n\n"
    )

    if not orders:
        text += "📭 No orders match this filter.\n\nTry a different filter or date range."
    else:
        for order in orders[:20]:
            status_config = STATUS_CONFIG.get(
                order.status,
                {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary"}
            )
            text += (
                f"{status_config['icon']} <b>#{order.id}</b> — "
                f"{order.product_name or 'Unknown'} | "
                f"${float(order.amount):.2f} | "
                f"{_format_timestamp(order.created_at, relative=True) if order.created_at else 'N/A'}\n"
            )

        if len(orders) > 20:
            text += f"\n<i>...and {len(orders) - 20} more</i>\n"

    keyboard = []
    for order in orders[:10]:
        status_config = STATUS_CONFIG.get(
            order.status,
            {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary"}
        )
        keyboard.append([
            InlineKeyboardButton(
                text=f"#{order.id} — {order.product_name or 'Unknown'}",
                callback_data=f"order_detail_{order.id}",
                style=status_config["style"]
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="🔄 Clear Filter",
            callback_data="orders_menu",
            style="danger"
        ),
        InlineKeyboardButton(
            text="🔍 New Filter",
            callback_data="order_filter_menu",
            style="primary"
        )
    ])

    await show(callback, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# ╔══════════════════════════════════════════════════════════════╗
# ║              ORDER SEARCH BY ID                             ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "order_search_by_id")
async def order_search_by_id(callback: CallbackQuery, state: FSMContext):
    """Prompt user to enter an order ID."""
    await callback.answer()
    await state.set_state(OrderFilterStates.waiting_filter)

    await show(
        callback,
        (
            f"🔍 <b>SEARCH ORDER</b>\n\n"
            f"🔢 <b>Enter the Order ID:</b>\n\n"
            f"{_divider('─')}\n\n"
            f"💡 <i>Example: 42</i>\n"
            f"❌ <i>Send 'cancel' to go back</i>"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data="orders_menu",
                        style="danger"
                    )
                ]
            ]
        ),
        state=state
    )


@router.message(OrderFilterStates.waiting_filter)
async def handle_order_id_search(message: Message, state: FSMContext):
    """Search for a specific order by ID."""
    await state.clear()

    if message.text and message.text.lower() in ["cancel", "exit", "back"]:
        await answer_card(message, "🔍 Search cancelled.")
        return

    try:
        order_id = int(message.text.strip())
    except ValueError:
        await answer_card(message, "❌ Please enter a valid order ID number.")
        return

    db = SessionLocal()
    try:
        order = (
            db.query(Order)
            .filter(
                Order.id == order_id,
                Order.telegram_id == message.from_user.id
            )
            .first()
        )
    finally:
        db.close()

    if not order:
        await answer_card(message,
            f"❌ <b>Order #{order_id} Not Found</b>\n\n"
            "It may belong to another user or doesn't exist.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📋 View All Orders",
                            callback_data="orders_menu",
                            style="primary"
                        )
                    ]
                ]
            )
        )
        return

    rating = await asyncio.to_thread(
        _load_order_rating, order.id, message.from_user.id
    )
    text = _format_order(order, detailed=True, rating=rating)

    await answer_card(message,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📎 Receipt",
                        callback_data=f"order_receipt_{order.id}",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="📋 All Orders",
                        callback_data="orders_menu",
                        style="primary"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              EXPORT ORDERS                                  ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "order_export_menu")
async def order_export_menu(callback: CallbackQuery):
    """Show export options."""
    await callback.answer()

    text = (
        f"📤 <b>EXPORT ORDERS</b>\n\n"
        f"📊 <b>Export your order history:</b>\n\n"
        f"{_divider('─')}\n\n"
        f"  📄 <b>CSV Format</b> — Open in Excel\n"
        f"  📝 <b>Text Format</b> — Readable summary\n\n"
        f"{_divider('─')}\n\n"
        f"💡 <i>Your data is never shared.</i>"
    )

    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Export as CSV",
                    callback_data="export_format_csv",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="📝 Export as Text",
                    callback_data="export_format_text",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back to Orders",
                    callback_data="orders_menu",
                    style="primary"
                )
            ]
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)


@router.callback_query(F.data.startswith("export_format_"))
async def export_orders(callback: CallbackQuery):
    """Export orders in the selected format."""
    await callback.answer()

    format_type = callback.data.replace("export_format_", "")

    db = SessionLocal()
    try:
        orders = (
            db.query(Order)
            .filter(Order.telegram_id == callback.from_user.id)
            .order_by(Order.id.desc())
            .all()
        )
    finally:
        db.close()

    if not orders:
        await callback.answer("📭 No orders to export.", show_alert=True)
        return

    if format_type == "csv":
        # Build CSV
        csv_lines = ["Order ID,Product,Quantity,Amount,Status,Delivery Type,Date"]
        for o in orders:
            date_str = o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "N/A"
            csv_lines.append(
                f"{o.id},{o.product_name or 'Unknown'},{o.quantity or 1},"
                f"{float(o.amount):.2f},{o.status},{o.delivery_type or 'N/A'},{date_str}"
            )
        export_text = "\n".join(csv_lines)
        caption = f"📄 <b>Orders Export (CSV)</b>\n📦 {len(orders)} orders"

    else:
        # Text format
        export_text = "ORDER HISTORY EXPORT\n" + "═" * 30 + "\n\n"
        total_spent = 0
        for o in orders:
            if o.status not in ["refunded", "deleted"]:
                total_spent += float(o.amount)
            status_config = STATUS_CONFIG.get(o.status, {"emoji": "❓", "label": o.status})
            date_str = o.created_at.strftime("%d %b %Y") if o.created_at else "N/A"
            export_text += (
                f"#{o.id} | {o.product_name or 'Unknown'} | "
                f"${float(o.amount):.2f} | {status_config['emoji']} {status_config['label']} | {date_str}\n"
            )
        export_text += f"\n{'═' * 30}\nTotal Spent: ${total_spent:.2f}\nOrders: {len(orders)}"
        caption = f"📝 <b>Orders Export (Text)</b>\n📦 {len(orders)} orders"

    # Truncate if too long
    if len(export_text) > 3800:
        export_text = export_text[:3800] + "\n\n... [truncated]"

    await show(
        callback,
        f"<pre>{export_text}</pre>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 Back to Orders",
                        callback_data="orders_menu",
                        style="primary"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              ADVANCED ANALYTICS                             ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "order_analytics")
async def order_analytics(callback: CallbackQuery):
    """Show advanced order analytics."""
    await callback.answer()

    db = SessionLocal()
    try:
        user_id = callback.from_user.id

        orders = (
            db.query(Order)
            .filter(Order.telegram_id == user_id)
            .order_by(Order.id.desc())
            .all()
        )

        if len(orders) < 2:
            await callback.answer("📊 Need at least 2 orders for analytics.", show_alert=True)
            return

        # Calculate metrics
        total_orders = len(orders)
        completed = sum(1 for o in orders if o.status == "completed")
        total_spent = sum(float(o.amount) for o in orders if o.status not in ["refunded", "deleted"])

        # Spending by month
        monthly_spend = {}
        for o in orders:
            if o.created_at and o.status not in ["refunded", "deleted"]:
                month_key = o.created_at.strftime("%b %Y")
                monthly_spend[month_key] = monthly_spend.get(month_key, 0) + float(o.amount)

        # Product frequency
        product_counts = {}
        for o in orders:
            name = o.product_name or "Unknown"
            product_counts[name] = product_counts.get(name, 0) + (o.quantity or 1)

        # Delivery preference
        delivery_counts = {}
        for o in orders:
            dt = o.delivery_type or "standard"
            delivery_counts[dt] = delivery_counts.get(dt, 0) + 1

        # Order frequency
        if len(orders) >= 2 and orders[0].created_at and orders[-1].created_at:
            days_span = (orders[-1].created_at - orders[0].created_at).days or 1
            orders_per_week = (total_orders / days_span) * 7
        else:
            orders_per_week = 0

    finally:
        db.close()

    text = (
        f"📊 <b>ANALYTICS DASHBOARD</b>\n\n"
        f"{_divider('═')}\n"
        f"📈 <b>OVERVIEW</b>\n"
        f"{_divider('═')}\n\n"
        f"📦 Total Orders: <b>{total_orders}</b>\n"
        f"✅ Completion Rate: <b>{int(completed / total_orders * 100)}%</b>\n"
        f"💰 Total Spent: <b>${total_spent:.2f}</b>\n"
        f"📊 Avg Order: <b>${total_spent / total_orders:.2f}</b>\n"
        f"📅 Orders/Week: <b>{orders_per_week:.1f}</b>\n\n"
    )

    if monthly_spend:
        text += f"{_divider('─')}\n📅 <b>MONTHLY SPENDING</b>\n"
        for month, amount in sorted(monthly_spend.items())[-6:]:
            bar_length = min(int(amount / max(monthly_spend.values()) * 10), 10)
            bar = "█" * bar_length + "░" * (10 - bar_length)
            text += f"  {month}: {bar} ${amount:.2f}\n"

    if product_counts:
        text += f"\n{_divider('─')}\n🏆 <b>MOST PURCHASED</b>\n"
        for name, count in sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
            text += f"  🛒 {name}: {count}x\n"

    if delivery_counts:
        delivery_labels = {
            "automatic": "🤖 Auto",
            "manual": "👨‍💼 Manual",
            "hybrid": "🔀 Hybrid",
            "standard": "📦 Standard"
        }
        text += f"\n{_divider('─')}\n🚚 <b>DELIVERY PREFERENCE</b>\n"
        for dt, count in delivery_counts.items():
            label = delivery_labels.get(dt, dt)
            pct = int(count / total_orders * 100)
            text += f"  {label}: {count}x ({pct}%)\n"

    text += f"\n{_divider('═')}\n\n💡 <i>Keep shopping to unlock more insights!</i>"

    await show(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Export Data",
                        callback_data="order_export_menu",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="📋 Back to Orders",
                        callback_data="orders_menu",
                        style="primary"
                    )
                ]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              VIEW ALL ORDERS                                ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "orders_view_all")
async def orders_view_all(callback: CallbackQuery):
    """Show all orders in a simplified list view."""
    await callback.answer()

    db = SessionLocal()
    try:
        user_id = callback.from_user.id

        orders = (
            db.query(Order)
            .filter(Order.telegram_id == user_id)
            .order_by(Order.id.desc())
            .all()
        )
    finally:
        db.close()

    if not orders:
        await show(callback, "📦 No orders found.")
        return

    text = (
        f"📋 <b>ALL ORDERS</b>\n\n"
        f"📦 <b>{len(orders)} total orders</b>\n\n"
    )

    for order in orders:
        status_config = STATUS_CONFIG.get(
            order.status,
            {"emoji": "❓", "label": order.status, "icon": "⚪", "style": "primary"}
        )

        date_str = _format_timestamp(order.created_at, relative=True) if order.created_at else "N/A"

        text += (
            f"{status_config['icon']} "
            f"<b>#{order.id}</b> — "
            f"{order.product_name or 'Unknown'}\n"
            f"   {status_config['emoji']} {status_config['label']} | "
            f"${float(order.amount):.2f} | {date_str}\n\n"
        )

    await show(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📊 Compact View",
                        callback_data="orders_menu",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="📤 Export",
                        callback_data="order_export_menu",
                        style="success"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅ Back to Menu",
                        callback_data="main_menu",
                        style="primary"
                    )
                ]
            ]
        )
    )
