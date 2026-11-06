import asyncio
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, and_

from database import SessionLocal
from models.ticket import Ticket
from models.user import User
from config import ADMIN_IDS
from states.support import SupportState
from utils.ui import show, update_card, answer_card

router = Router()

# Store only the Telegram username, without @ or a t.me URL.
CONTACT_USERNAME = "Raindrop7707"


# =====================================================
# UTILITY FUNCTIONS
# =====================================================

def get_status_emoji(status: str) -> str:
    return {
        "Open": "🆕",
        "In Progress": "🔄",
        "Resolved": "✅",
        "Closed": "❌",
        "Reopened": "🔄"
    }.get(status, "📋")


def get_priority_emoji(priority: str) -> str:
    return {
        "Low": "🟢",
        "Medium": "🟡",
        "High": "🟠",
        "Urgent": "🔴"
    }.get(priority, "🟡")


# =====================================================
# CONTACT
# =====================================================

@router.callback_query(F.data == "contact_info")
async def contact(callback: CallbackQuery):
    """Contact admin with colored buttons."""
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📞 Open Direct Chat",
                    url=f"https://t.me/{CONTACT_USERNAME}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Create Support Ticket",
                    callback_data="support_create_ticket",
                    style="primary"  # 🔵 Blue
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 My Tickets",
                    callback_data="support_my_tickets",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Main Menu",
                    callback_data="main_menu"
                )
            ]
        ]
    )

    await show(
        callback,
        "📞 <b>Contact Admin</b>\n\n"
        "Click below to open a direct chat with our support team.\n\n"
        "💡 <i>For complex issues, create a support ticket.</i>",
        reply_markup=markup
    )


# =====================================================
# SUPPORT MENU
# =====================================================

@router.callback_query(F.data == "support_ticket")
async def support_menu(callback: CallbackQuery):
    """Main support menu with colored buttons."""
    await callback.answer()

    db = SessionLocal()
    try:
        open_tickets = (
            db.query(func.count(Ticket.id))
            .filter(
                and_(
                    Ticket.user_id == callback.from_user.id,
                    Ticket.status.in_(["Open", "In Progress"])
                )
            )
            .scalar()
        ) or 0

        resolved_tickets = (
            db.query(func.count(Ticket.id))
            .filter(
                and_(
                    Ticket.user_id == callback.from_user.id,
                    Ticket.status == "Resolved"
                )
            )
            .scalar()
        ) or 0

        total_tickets = (
            db.query(func.count(Ticket.id))
            .filter(Ticket.user_id == callback.from_user.id)
            .scalar()
        ) or 0

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎫 Create New Ticket",
                        callback_data="support_create_ticket",
                        style="success"  # 🟢 Green — positive action
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"📋 My Tickets ({total_tickets})",
                        callback_data="support_my_tickets",
                        style="primary"  # 🔵 Blue
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔍 Track Ticket",
                        callback_data="support_track_ticket",
                        style="primary"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📞 Direct Contact",
                        callback_data="contact_info"
                    ),
                    InlineKeyboardButton(
                        text="❓ FAQ",
                        callback_data="support_faq",
                        style="primary"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⭐ Rate Support",
                        callback_data="support_rate",
                        style="primary"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅ Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ]
        )

        stats_text = ""
        if total_tickets > 0:
            resolution_rate = (resolved_tickets / total_tickets) * 100
            stats_text = (
                f"📊 <b>Your Support Stats</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟡 Open: {open_tickets}  |  "
                f"🟢 Resolved: {resolved_tickets}  |  "
                f"📊 Total: {total_tickets}\n"
                f"✅ Resolution Rate: {resolution_rate:.0f}%\n\n"
            )

        await show(
            callback,
            f"🎫 <b>Support Center</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{stats_text}"
            f"Choose an option below:\n\n"
            f"🟢 <b>Create Ticket</b> — Report an issue\n"
            f"🔵 <b>My Tickets</b> — View your tickets\n"
            f"🔵 <b>Track Ticket</b> — Find a ticket\n"
            f"🔵 <b>FAQ</b> — Common answers\n\n"
            f"💡 <i>We respond within 2 hours!</i>",
            reply_markup=markup
        )
    finally:
        db.close()


# =====================================================
# TRACK SPECIFIC TICKET
# =====================================================

@router.callback_query(F.data == "support_track_ticket")
async def track_ticket_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SupportState.tracking_ticket)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="support_ticket",
                    style="danger"  # 🔴 Red — cancel action
                )
            ]
        ]
    )

    await show(
        callback,
        "🔍 <b>Track Your Ticket</b>\n\n"
        "Please enter your ticket ID to check its status.\n\n"
        "<i>Example: 42</i>",
        reply_markup=markup,
        state=state
    )


@router.message(SupportState.tracking_ticket)
async def show_ticket_details(message: Message, state: FSMContext):
    await state.clear()

    try:
        ticket_id = int(message.text.strip())
    except ValueError:
        await answer_card(message, "❌ Invalid ticket ID. Please enter a number.")
        return

    db = SessionLocal()
    try:
        ticket = (
            db.query(Ticket)
            .filter(
                and_(
                    Ticket.id == ticket_id,
                    Ticket.user_id == message.from_user.id
                )
            )
            .first()
        )

        if not ticket:
            await answer_card(message,
                "❌ <b>Ticket Not Found</b>\n\n"
                "This ticket doesn't exist or doesn't belong to you."
            )
            return

        status_emoji = get_status_emoji(ticket.status)
        priority_emoji = get_priority_emoji(ticket.priority or "Medium")
        created = ticket.created_at.strftime("%d %b %Y, %H:%M UTC") if ticket.created_at else "N/A"
        updated = ticket.updated_at.strftime("%d %b %Y, %H:%M UTC") if ticket.updated_at else "N/A"

        text = (
            f"🎫 <b>Ticket #{ticket.id} Details</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📂 <b>Category:</b> {ticket.category or 'General'}\n"
            f"{priority_emoji} <b>Priority:</b> {ticket.priority or 'Medium'}\n"
            f"📊 <b>Status:</b> {status_emoji} {ticket.status}\n"
            f"🕐 <b>Created:</b> {created}\n"
            f"🔄 <b>Updated:</b> {updated}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Your Message:</b>\n"
            f"{ticket.message}\n"
        )

        if ticket.admin_response:
            text += (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💬 <b>Admin Response:</b>\n"
                f"{ticket.admin_response}\n"
            )

        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(
                text="📋 All My Tickets",
                callback_data="support_my_tickets",
                style="primary"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="⬅ Support Menu",
                callback_data="support_ticket"
            )
        )

        await answer_card(message, text, reply_markup=builder.as_markup())

    finally:
        db.close()


# =====================================================
# FAQ
# =====================================================

@router.callback_query(F.data == "support_faq")
async def support_faq(callback: CallbackQuery):
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Payments & Deposits",
                    callback_data="faq_payments",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Orders & Delivery",
                    callback_data="faq_orders",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Referrals & Earnings",
                    callback_data="faq_referrals",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔒 Account & Security",
                    callback_data="faq_account",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Support & Tickets",
                    callback_data="faq_support",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎫 Still Need Help? Create Ticket",
                    callback_data="support_create_ticket",
                    style="success"  # 🟢 Green — helpful action
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="support_ticket"
                )
            ]
        ]
    )

    await show(
        callback,
        "❓ <b>Frequently Asked Questions</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Select a category to find answers:\n\n"
        "💡 <i>Can't find your answer? Create a ticket!</i>",
        reply_markup=markup
    )


@router.callback_query(F.data.startswith("faq_"))
async def faq_category(callback: CallbackQuery):
    await callback.answer()

    faq_content = {
        "faq_payments": (
            "💰 <b>Payments & Deposits</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "💳 <b>How to deposit?</b>\n"
            "• Go to Wallet → Deposit\n"
            "• Choose your payment method\n"
            "• Minimum deposit: $5.00\n\n"
            "⏱️ <b>Processing time?</b>\n"
            "• Crypto: 10-30 min\n"
            "• UPI/Pay: 5-15 min\n"
        ),
        "faq_orders": (
            "📦 <b>Orders & Delivery</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🛒 <b>How to order?</b>\n"
            "• Browse Products in shop\n"
            "• Complete payment\n\n"
            "⏱️ <b>Delivery time?</b>\n"
            "• Digital: Instant\n"
            "• Services: 24-48 hours\n"
        ),
        "faq_referrals": (
            "👥 <b>Referrals & Earnings</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔗 <b>How to refer?</b>\n"
            "• Go to Referrals menu\n"
            "• Share your referral link\n\n"
            "💰 <b>Earnings?</b>\n"
            "• 10% of referrals' activity\n"
            "• No earning limits\n"
        ),
        "faq_account": (
            "🔒 <b>Account & Security</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 <b>Lost access?</b>\n"
            "• Use /start to refresh\n\n"
            "🚫 <b>Banned?</b>\n"
            "• Open a ticket to appeal\n"
        ),
        "faq_support": (
            "🎫 <b>Support & Tickets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🎫 <b>Create a ticket?</b>\n"
            "• Support → Create Ticket\n\n"
            "⏱️ <b>Response time?</b>\n"
            "• Under 2 hours\n"
        ),
    }

    content = faq_content.get(callback.data, "❓ FAQ not found.")

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎫 Create Ticket",
                    callback_data="support_create_ticket",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back to FAQ",
                    callback_data="support_faq"
                ),
                InlineKeyboardButton(
                    text="🏠 Support Menu",
                    callback_data="support_ticket"
                )
            ]
        ]
    )

    await show(callback, content, reply_markup=markup)


# =====================================================
# RATE SUPPORT
# =====================================================

@router.callback_query(F.data == "support_rate")
async def rate_support(callback: CallbackQuery):
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="😡 Terrible",
                    callback_data="rate_1",
                    style="danger"  # 🔴 Red
                )
            ],
            [
                InlineKeyboardButton(
                    text="😐 Poor",
                    callback_data="rate_2",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="😊 Good",
                    callback_data="rate_3",
                    style="primary"  # 🔵 Blue
                )
            ],
            [
                InlineKeyboardButton(
                    text="😄 Great",
                    callback_data="rate_4",
                    style="success"  # 🟢 Green
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌟 Excellent",
                    callback_data="rate_5",
                    style="success"  # 🟢 Green
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="support_ticket"
                )
            ]
        ]
    )

    await show(
        callback,
        "⭐ <b>Rate Our Support</b>\n\n"
        "How would you rate your support experience?\n\n"
        "Your feedback helps us improve! 🙏",
        reply_markup=markup
    )


@router.callback_query(F.data.in_({"rate_1", "rate_2", "rate_3", "rate_4", "rate_5"}))
async def handle_rating(callback: CallbackQuery):
    await callback.answer()

    rating = int(callback.data.split("_")[1])
    rating_emoji = {1: "😡", 2: "😐", 3: "😊", 4: "😄", 5: "🌟"}
    rating_text = {1: "Terrible", 2: "Poor", 3: "Good", 4: "Great", 5: "Excellent"}

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Support Menu",
                    callback_data="support_ticket",
                    style="primary"
                )
            ]
        ]
    )

    await show(
        callback,
        f"{rating_emoji[rating]} <b>Thanks for your feedback!</b>\n\n"
        f"You rated our support: <b>{rating_text[rating]}</b>\n\n"
        "We appreciate your time! 🙏",
        reply_markup=markup
    )


# =====================================================
# CREATE TICKET — WITH COLORED CATEGORIES
# =====================================================

@router.callback_query(F.data == "support_create_ticket")
async def ticket_categories(callback: CallbackQuery):
    """Show ticket categories with colored buttons."""
    await callback.answer()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 Bug Report",
                    callback_data="ticket_cat_bug",
                    style="danger"  # 🔴 Red — urgent
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟠 Order Problem",
                    callback_data="ticket_cat_order",
                    style="primary"  # 🔵 Blue
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟡 Payment Issue",
                    callback_data="ticket_cat_payment",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟡 Account Help",
                    callback_data="ticket_cat_account",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Suggestion",
                    callback_data="ticket_cat_suggestion",
                    style="success"  # 🟢 Green — positive
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 General Question",
                    callback_data="ticket_cat_other",
                    style="success"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="support_ticket"
                )
            ]
        ]
    )

    await show(
        callback,
        "🎫 <b>Create Support Ticket</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📂 <b>Select your issue category:</b>\n\n"
        "🔴 <b>Urgent</b> — Bug Reports\n"
        "🔵 <b>Priority</b> — Orders, Payments, Account\n"
        "🟢 <b>General</b> — Suggestions, Questions\n\n"
        "<i>Select the most relevant category for faster help!</i>",
        reply_markup=markup
    )


CATEGORY_PRIORITY = {
    "ticket_cat_payment": "Medium",
    "ticket_cat_order": "High",
    "ticket_cat_account": "Medium",
    "ticket_cat_bug": "Urgent",
    "ticket_cat_suggestion": "Low",
    "ticket_cat_other": "Low",
}


@router.callback_query(F.data.startswith("ticket_cat_"))
async def select_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    category_map = {
        "ticket_cat_payment": "💰 Payment Issue",
        "ticket_cat_order": "📦 Order Problem",
        "ticket_cat_account": "👤 Account Help",
        "ticket_cat_bug": "🔧 Bug Report",
        "ticket_cat_suggestion": "💡 Suggestion",
        "ticket_cat_other": "📋 General Question",
    }

    category = category_map.get(callback.data, "📋 General Question")
    priority = CATEGORY_PRIORITY.get(callback.data, "Medium")
    priority_emoji = get_priority_emoji(priority)

    await state.update_data(ticket_category=category, ticket_priority=priority)
    await state.set_state(SupportState.waiting_message)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="support_ticket",
                    style="danger"  # 🔴 Red
                )
            ]
        ]
    )

    await show(
        callback,
        f"🎫 <b>Create Ticket</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📂 <b>Category:</b> {category}\n"
        f"{priority_emoji} <b>Priority:</b> {priority}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Describe your issue:</b>\n\n"
        f"<i>Please include relevant details.</i>\n\n"
        f"✍️ <b>Send your message now:</b>",
        reply_markup=markup,
        state=state,
    )


def _save_ticket(user_id: int, text: str, category: str, priority: str) -> int:
    db = SessionLocal()
    try:
        ticket = Ticket(
            user_id=user_id,
            message=text,
            category=category,
            priority=priority,
            status="Open",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return ticket.id
    finally:
        db.close()


@router.message(SupportState.waiting_message)
async def create_ticket(message: Message, state: FSMContext):
    data = await state.get_data()
    card_chat_id = data.get("_card_chat_id")
    card_message_id = data.get("_card_message_id")
    category = data.get("ticket_category", "📋 General Question")
    priority = data.get("ticket_priority", "Medium")
    priority_emoji = get_priority_emoji(priority)

    if len(message.text) < 20:
        await answer_card(message,
            "⚠️ <b>Message too short!</b>\n\n"
            "Please provide more details (minimum 20 characters)."
        )
        return

    if len(message.text) > 2000:
        await answer_card(message,
            "⚠️ <b>Message too long!</b>\n\n"
            "Please keep your message under 2000 characters."
        )
        return

    ticket_id = await asyncio.to_thread(
        _save_ticket, message.from_user.id, message.text, category, priority
    )

    await state.clear()

    await update_card(
        message, None,
        f"✅ <b>Ticket Created Successfully!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎫 <b>Ticket ID:</b> #{ticket_id}\n"
        f"📂 <b>Category:</b> {category}\n"
        f"{priority_emoji} <b>Priority:</b> {priority}\n"
        f"📊 <b>Status:</b> 🆕 Open\n\n"
        f"⏱️ Our team will review your ticket.\n"
        f"💡 Track your ticket in 📋 My Tickets",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 View My Tickets",
                        callback_data="support_my_tickets",
                        style="primary"
                    ),
                    InlineKeyboardButton(
                        text="🔍 Track This Ticket",
                        callback_data="support_track_ticket",
                        style="primary"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅ Main Menu",
                        callback_data="main_menu"
                    )
                ]
            ]
        ),
        chat_id=card_chat_id,
        message_id=card_message_id,
    )

    # Notify admins
    from services.notification_outbox import admin_destination
    for admin in filter(None, [admin_destination()]):
        try:
            username = (
                f"@{message.from_user.username}"
                if message.from_user.username
                else "No username"
            )

            admin_markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ In Progress",
                            callback_data=f"admin_ticket_progress_{ticket_id}",
                            style="primary"  # 🔵 Blue
                        ),
                        InlineKeyboardButton(
                            text="✅ Resolved",
                            callback_data=f"admin_ticket_resolve_{ticket_id}",
                            style="success"  # 🟢 Green
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Close",
                            callback_data=f"admin_ticket_close_{ticket_id}",
                            style="danger"  # 🔴 Red
                        ),
                        InlineKeyboardButton(
                            text="💬 Reply",
                            callback_data=f"admin_ticket_reply_{ticket_id}",
                            style="primary"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="👤 View User",
                            url=f"tg://user?id={message.from_user.id}"
                        )
                    ]
                ]
            )

            from services.notification_outbox import queue_message
            await queue_message(message.bot, f"admin-ticket:{ticket_id}",
                admin,
                f"🔔 <b>New Support Ticket #{ticket_id}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📂 <b>Category:</b> {category}\n"
                f"{priority_emoji} <b>Priority:</b> {priority}\n"
                f"👤 <b>User:</b> {username}\n"
                f"🆔 <b>User ID:</b> <code>{message.from_user.id}</code>\n"
                f"🕐 <b>Time:</b> {datetime.utcnow().strftime('%d %b %Y, %H:%M UTC')}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Message:</b>\n\n"
                f"{message.text[:800]}{'...' if len(message.text) > 800 else ''}",
                reply_markup=admin_markup
            )
        except Exception as e:
            print("Admin ticket notification error:", e)


# =====================================================
# MY TICKETS — WITH COLORED BUTTONS
# =====================================================

@router.callback_query(F.data == "support_my_tickets")
async def my_tickets(callback: CallbackQuery):
    await callback.answer()

    db = SessionLocal()
    try:
        tickets = (
            db.query(Ticket)
            .filter(Ticket.user_id == callback.from_user.id)
            .order_by(Ticket.updated_at.desc())
            .limit(15)
            .all()
        )

        if not tickets:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎫 Create Your First Ticket",
                            callback_data="support_create_ticket",
                            style="success"  # 🟢 Green
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅ Support Menu",
                            callback_data="support_ticket"
                        )
                    ]
                ]
            )

            await show(
                callback,
                "📋 <b>No Tickets Yet</b>\n\n"
                "You haven't created any support tickets.\n\n"
                "Need help? Create a ticket!",
                reply_markup=markup
            )
            return

        open_tickets = [t for t in tickets if t.status in ["Open", "In Progress"]]
        closed_tickets = [t for t in tickets if t.status in ["Resolved", "Closed"]]

        text = "📋 <b>Your Support Tickets</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

        if open_tickets:
            text += "🟡 <b>Active Tickets:</b>\n"
            for ticket in open_tickets:
                status_emoji = get_status_emoji(ticket.status)
                priority_emoji = get_priority_emoji(ticket.priority or "Medium")
                created = ticket.created_at.strftime("%d/%m/%y")
                text += (
                    f"{status_emoji} <b>#{ticket.id}</b> {priority_emoji} "
                    f"<i>{created}</i> — {ticket.category or 'General'}\n"
                )
            text += "\n"

        if closed_tickets:
            text += "✅ <b>Resolved Tickets:</b>\n"
            for ticket in closed_tickets[:5]:
                status_emoji = get_status_emoji(ticket.status)
                created = ticket.created_at.strftime("%d/%m/%y")
                text += (
                    f"{status_emoji} <b>#{ticket.id}</b> <i>{created}</i>"
                    f" — {ticket.category or 'General'}\n"
                )
            text += "\n"

        text += (
            f"📊 <b>Summary:</b> "
            f"Active: {len(open_tickets)} | "
            f"Resolved: {len(closed_tickets)} | "
            f"Total: {len(tickets)}\n"
        )

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎫 New Ticket",
                        callback_data="support_create_ticket",
                        style="success"  # 🟢 Green
                    ),
                    InlineKeyboardButton(
                        text="🔍 Track by ID",
                        callback_data="support_track_ticket",
                        style="primary"  # 🔵 Blue
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅ Support Menu",
                        callback_data="support_ticket"
                    )
                ]
            ]
        )

        await show(callback, text, reply_markup=markup)
    finally:
        db.close()


# =====================================================
# ADMIN COMMANDS — COLORED ACTIONS
# =====================================================

@router.callback_query(F.data.startswith("admin_ticket_"))
async def admin_ticket_action(callback: CallbackQuery):
    await callback.answer()

    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin access only.", show_alert=True)
        return

    parts = callback.data.split("_")
    action = parts[2]
    ticket_id = int(parts[3])

    db = SessionLocal()
    try:
        ticket = db.query(Ticket).filter(Ticket.id == ticket_id).first()

        if not ticket:
            await callback.answer("❌ Ticket not found.", show_alert=True)
            return

        status_map = {
            "progress": "In Progress",
            "resolve": "Resolved",
            "close": "Closed",
        }

        if action in status_map:
            old_status = ticket.status
            new_status = status_map[action]
            ticket.status = new_status
            ticket.updated_at = datetime.utcnow()
            db.commit()

            try:
                user = db.query(User).filter(User.id == ticket.user_id).first()
                if user:
                    notify_text = (
                        f"🔔 <b>Ticket #{ticket.id} Updated</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📊 <b>Status:</b> {old_status} → {new_status}\n\n"
                    )

                    if new_status == "Resolved":
                        notify_text += "✅ Your issue has been resolved!\nPlease rate our support. ⭐"
                    elif new_status == "In Progress":
                        notify_text += "🔄 Our team is working on your issue."
                    elif new_status == "Closed":
                        notify_text += "❌ This ticket has been closed."

                    notify_markup = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📋 View Tickets",
                                    callback_data="support_my_tickets",
                                    style="primary"
                                ),
                                InlineKeyboardButton(
                                    text="⭐ Rate Support",
                                    callback_data="support_rate",
                                    style="success"
                                )
                            ]
                        ]
                    )

                    await callback.bot.send_message(
                        user.telegram_id,
                        notify_text,
                        reply_markup=notify_markup
                    )
            except Exception as e:
                print(f"Failed to notify user {ticket.user_id}:", e)

            await callback.message.edit_text(
                callback.message.text + "\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>Status:</b> {new_status}",
                reply_markup=None
            )

            await callback.answer(f"✅ Ticket #{ticket_id}: {new_status}", show_alert=True)

    finally:
        db.close()
