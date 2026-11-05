
from utils.ui import answer_card, schedule_success_notice, show
from services.order_workflow import record_event
from services.warranty import start_coverage
from services.notification_outbox import enqueue, queue_message
from datetime import datetime
import asyncio
import logging
from html import escape
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext

from sqlalchemy.exc import SQLAlchemyError

import config
from config import ADMIN_IDS
from database import SessionLocal, transaction, retry_on_write_conflict
from services.delivery_bridge import DeliveryBridgeError, send_manual_delivery
from models.api_key import ApiOrder
from models.order import Order
from models.user import User
from models.product import Product
from models.order_requirement_submission import OrderRequirementSubmission
from models.admin_audit_log import AdminAuditLog
from states.delivery_states import DeliverOrder

from handlers.products import (
    REFERRAL_COMMISSION_RATE,
    REFERRAL_CREDIT_TO_BALANCE,
    _money,
)

logger = logging.getLogger(__name__)

router = Router()

STATUS_LABELS = {
    "completed": "✅ Completed",
    "pending_manual": "⏳ Pending (manual fulfillment)",
    "pending_info": "🧾 Waiting for buyer information",
    "preorder": "📦 Preorder (waitlisted)",
    "refunded": "💸 Refunded",
    "deleted": "🗑 Deleted",
}
ADMIN_ORDERS_PER_PAGE = 10


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ==========================================================
# ADMIN ORDERS LIST
# ==========================================================

def _load_admin_orders(page):
    with SessionLocal() as db:
        query = db.query(Order).filter(Order.status != "deleted").order_by(Order.id.desc())
        total = query.count()
        pages = max(1, (total + ADMIN_ORDERS_PER_PAGE - 1) // ADMIN_ORDERS_PER_PAGE)
        page = min(page, pages - 1)
        orders = query.offset(page * ADMIN_ORDERS_PER_PAGE).limit(ADMIN_ORDERS_PER_PAGE).all()
        return orders, total, pages, page


def _load_admin_order(order_id):
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if not order:
            return None, None, {}
        user = db.query(User).filter(User.telegram_id == order.telegram_id).first()
        submission = db.query(OrderRequirementSubmission).filter_by(order_id=order_id).first()
        return order, user, submission.values if submission else {}


@router.callback_query(F.data == "admin_orders")
@router.callback_query(F.data.startswith("admin_orders_page_"))
async def admin_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    try:
        page = int(callback.data.rsplit("_", 1)[-1]) if callback.data != "admin_orders" else 0
        page = max(0, page)
    except ValueError:
        page = 0

    await callback.answer()
    orders, total, total_pages, page = await asyncio.to_thread(_load_admin_orders, page)

    if not orders:
        await show(callback,
            "❌ No orders found.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Fulfillment queue", callback_data="work_queue_all_0")],
                    [InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")]
                ]
            )
        )
        return

    keyboard = []
    for order in orders:
        if order.refunded:
            icon = "💸"
        elif order.status == "completed":
            icon = "✅"
        elif order.status == "preorder":
            icon = "📦"
        else:
            icon = "⏳"

        keyboard.append([
            InlineKeyboardButton(
                text=f"#{order.id} {icon} {order.product_name}",
                callback_data=f"order_{order.id}_{page}"
            )
        ])

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"admin_orders_page_{page - 1}"))
    if page < total_pages - 1:
        navigation.append(InlineKeyboardButton(text="Next ➡", callback_data=f"admin_orders_page_{page + 1}"))
    if navigation:
        keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton(text="📋 Fulfillment queue", callback_data="work_queue_all_0")])
    keyboard.append([InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")])

    await show(callback,
        f"📦 Orders ({page + 1}/{total_pages}) — {total} total",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )



# ==========================================================
# SINGLE ORDER
# ==========================================================

@router.callback_query(F.data.regexp(r"^order_\d+(?:_\d+)?$"))
async def order_info(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    parts = callback.data.split("_")
    order_id = int(parts[1])
    list_page = int(parts[2]) if len(parts) > 2 else 0

    await callback.answer()
    order, user, submitted_values = await asyncio.to_thread(_load_admin_order, order_id)
    if not order:
        await show(callback, "Order not found.")
        return
    username = f"@{user.username}" if user and user.username else "Not set"

    delivered = order.delivered_account or "Not delivered yet"
    status_label = STATUS_LABELS.get(order.status, order.status)

    text = f"""
📦 Order #{order.id}

👤 User ID:
<code>{order.telegram_id}</code>

🔎 Username:
{escape(username)}

📦 Product:
{escape(order.product_name or "Product")} x{order.quantity or 1}

🔑 Delivered:
<code>{escape(delivered)}</code>

💰 Amount:
${_money(order.amount):.2f}

📄 Status:
{status_label}

📅 Date:
{order.created_at}

Refunded:
{"✅ YES" if order.refunded else "❌ NO"}
"""

    photo_file_ids = []
    if submitted_values:
        text += "\n🧾 Buyer information:\n"
        for label, item in submitted_values.items():
            kind = item.get("kind") if isinstance(item, dict) else "text"
            if kind == "photo":
                photo_file_ids.append((label, item.get("file_id")))
                value = "Photo attached (shown below)"
            elif kind == "contact":
                value = f"{item.get('first_name', '')} · {item.get('phone', '')}"
            elif isinstance(item, dict):
                value = item.get("value", "")
            else:
                value = str(item)
            text += f"• <b>{escape(str(label))}:</b> <code>{escape(str(value))}</code>\n"

    text += f"\nTeam owner: {order.assigned_admin_id or 'Unassigned'}\nAutomatic: {order.automatic_quantity or 0} · Remaining: {order.manual_pending_quantity or 0}\n"
    keyboard = [[InlineKeyboardButton(text="🕒 Timeline", callback_data=f"admin_timeline_{order.id}"),
                 InlineKeyboardButton(text="📋 Team workspace", callback_data=f"work_view_{order.id}")]]
    if order.warranty_days:
        keyboard.append([InlineKeyboardButton(text='🛡 Warranty / replacement', callback_data=f'warranty_admin_{order.id}')])

    needs_fulfillment = (
        order.status in ("pending_manual", "preorder")
        and not order.refunded
    )
    if needs_fulfillment:
        keyboard.append([
            InlineKeyboardButton(text="📤 Deliver", callback_data=f"deliver_order_{order.id}")
        ])

    if not order.refunded and order.status != "deleted":
        keyboard.append([
            InlineKeyboardButton(text="💸 Refund", callback_data=f"refund_{order.id}")
        ])

    if order.status != "deleted":
        keyboard.append([
            InlineKeyboardButton(text="🗑 Delete", callback_data=f"delete_order_{order.id}")
        ])

    keyboard.append([InlineKeyboardButton(
        text="⬅ Back",
        callback_data="admin_orders" if list_page == 0 else f"admin_orders_page_{list_page}",
    )])

    await show(callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    for label, file_id in photo_file_ids:
        if file_id:
            await callback.message.answer_photo(file_id, caption=f"Order #{order.id} · {escape(str(label))}")



# ==========================================================
# REFUND — restores stock, reverses any referral commission
# already paid on this order, all in one transaction
# ==========================================================

@retry_on_write_conflict(max_attempts=3)
def _do_refund(order_id: int, admin_id: int) -> dict:
    with transaction() as db:
        order = (
            db.query(Order)
            .filter(Order.id == order_id)
            .with_for_update()
            .first()
        )

        if not order:
            return {"error": "Order not found."}
        if order.refunded:
            return {"error": "Already refunded."}
        if order.status == "deleted":
            return {"error": "This order was deleted and can no longer be refunded."}

        buyer = (
            db.query(User)
            .filter(User.telegram_id == order.telegram_id)
            .with_for_update()
            .first()
        )
        if not buyer:
            return {"error": "Buyer account not found."}

        refund_amount = _money(order.amount)

        # --- Restore stock -------------------------------------
        # Only automatically-delivered accounts can be meaningfully
        # "put back" (as text, appended back onto file_content) — a
        # manual-fulfillment unit that was hand-delivered has no
        # generic representation to restore, so for "manual"/hybrid
        # orders without a delivered_account we restore the numeric
        # stock counter instead. Preorders and never-fulfilled manual
        # orders never decremented stock in the first place, so there
        # is nothing to restore for those.
        product = (
            db.query(Product)
            .filter(Product.id == order.product_id)
            .with_for_update()
            .first()
        )

        stock_restored = False
        if product is not None and order.stock_deducted_quantity is not None:
            restored = (order.reserved_delivery or order.delivered_account or "").splitlines()[:order.automatic_quantity]
            existing = [line.strip() for line in (product.file_content or "").splitlines() if line.strip()]
            product.file_content = "\n".join(restored + existing) or None
            product.stock = (product.stock or 0) + order.stock_deducted_quantity
            order.reserved_delivery = None
            stock_restored = bool(order.stock_deducted_quantity)
        elif product is not None and not order.is_preorder:
            if order.reserved_delivery and order.status == "pending_info":
                restored_accounts = [
                    account.strip() for account in order.reserved_delivery.splitlines() if account.strip()
                ]
                existing = [
                    account.strip() for account in (product.file_content or "").splitlines() if account.strip()
                ]
                product.file_content = "\n".join(restored_accounts + existing)
                product.stock = len(restored_accounts) + len(existing)
                order.reserved_delivery = None
                stock_restored = bool(restored_accounts)
            elif order.delivered_account and order.status == "completed":
                # Automatic (or hybrid-auto) delivery: the exact
                # account strings were consumed from file_content —
                # put them back so they can be resold.
                restored_accounts = [
                    a.strip() for a in order.delivered_account.splitlines() if a.strip()
                ]
                if restored_accounts:
                    existing = (
                        [a.strip() for a in product.file_content.splitlines() if a.strip()]
                        if product.file_content else []
                    )
                    product.file_content = "\n".join(restored_accounts + existing)
                    product.stock = len(restored_accounts) + len(existing)
                    stock_restored = True
            elif order.status in ("pending_info", "pending_manual", "completed"):
                # Manual/hybrid-manual fulfillment: only the numeric
                # counter moved, so give the units back there.
                product.stock = (product.stock or 0) + (order.quantity or 1)
                stock_restored = True

        # --- Reverse referral commission, if one was paid ------
        # Commission is paid at purchase time for completed /
        # pending_manual orders (see handlers/products.py), or at
        # delivery time for a fulfilled preorder (see
        # deliver_order_finish below). Either way, by the time an
        # order can be refunded it is one of: completed,
        # pending_manual, or preorder-that-was-never-delivered (no
        # commission was ever paid on that last one).
        commission_reversed = None
        if order.status != "preorder" and buyer.referred_by:
            referrer = (
                db.query(User)
                .filter(User.telegram_id == buyer.referred_by)
                .with_for_update()
                .first()
            )
            if referrer is not None:
                commission = _money(refund_amount * REFERRAL_COMMISSION_RATE)
                if commission > 0:
                    referrer.referral_earnings = max(
                        Decimal("0"),
                        _money(referrer.referral_earnings) - commission,
                    )
                    if REFERRAL_CREDIT_TO_BALANCE:
                        referrer.balance = _money(referrer.balance) - commission
                    commission_reversed = {
                        "referrer_telegram_id": referrer.telegram_id,
                        "amount": commission,
                    }

        # --- Refund the buyer -----------------------------------
        buyer.balance = _money(buyer.balance) + refund_amount

        record_event(db, order.id, 'refunded', 'Order refunded to wallet.', actor_id=admin_id)
        enqueue(db, f'refund:{order.id}', order.telegram_id,
                f'💸 Your order #{order.id} was refunded.\n\nAmount returned to your balance: ${refund_amount:.2f}')
        order.refunded = True
        order.status = "refunded"
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id),
            action="refund_order",
            entity_type="order",
            entity_id=str(order.id),
            summary=f"Refunded ${refund_amount:.2f}; stock_restored={stock_restored}.",
        ))

        logger.info(
            "Order %s refunded by admin %s | buyer=%s amount=%s stock_restored=%s",
            order.id, admin_id, order.telegram_id, refund_amount, stock_restored,
        )

        return {
            "order_id": order.id,
            "buyer_id": order.telegram_id,
            "amount": refund_amount,
            "stock_restored": stock_restored,
            "commission_reversed": commission_reversed,
        }


@router.callback_query(F.data.startswith("refund_"))
async def refund_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[1])

    try:
        result = await _dispatch_blocking(_do_refund, order_id, callback.from_user.id)
    except SQLAlchemyError:
        logger.exception("Database error refunding order %s", order_id)
        await callback.answer("❌ Database error while refunding. Please try again.", show_alert=True)
        return
    except Exception:
        logger.exception("Unexpected error refunding order %s", order_id)
        await callback.answer("❌ Unexpected error while refunding.", show_alert=True)
        return

    if "error" in result:
        await callback.answer(result["error"], show_alert=True)
        return

    await callback.answer("✅ Refunded")

    callback.data = f"order_{order_id}"
    await order_info(callback)


# ==========================================================
# DELETE — soft delete only, never removes the row
# ==========================================================

@router.callback_query(F.data.startswith("delete_order_"))
async def delete_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()

        if not order:
            await callback.answer("Order not found.")
            return

        # Soft delete: keep the row (audit trail, refund history,
        # referral-commission bookkeeping) but hide it from the admin
        # orders list and stop offering further actions on it.
        record_event(db, order.id, "deleted", "Order archived by the team.", actor_id=callback.from_user.id, customer_visible=False)
        order.status = "deleted"
        db.add(AdminAuditLog(
            admin_telegram_id=int(callback.from_user.id), action="soft_delete_order",
            entity_type="order", entity_id=str(order.id), summary="Order marked deleted (soft delete).",
        ))
        db.commit()

        logger.info("Order %s soft-deleted by admin %s", order_id, callback.from_user.id)

    finally:
        db.close()

    deleted_notice = await show(callback,
        "✅ Order deleted.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📦 Orders", callback_data="admin_orders")]
            ]
        )
    )
    schedule_success_notice(deleted_notice, "✅ Order deleted.", owner_id=callback.from_user.id)
    await callback.answer()


# ==========================================================
# MANUAL DELIVERY (for "manual"/"hybrid" delivery type orders
# and preorders — admin sends the account/key by hand)
# ==========================================================

@router.callback_query(F.data.startswith("deliver_order_"))
async def deliver_order_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    order_id = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        order = db.get(Order, order_id)
        is_api_order = bool(order and db.query(ApiOrder.id).filter(ApiOrder.order_id == order.id).first())
        if order and order.delivery_type in {"manual", "hybrid"} and not (
            config.DELIVERY_BRIDGE_URL and config.DELIVERY_BRIDGE_SECRET
        ):
            await callback.answer(
                "The Cloudflare delivery bridge is not configured. Ask the manager to configure it before delivery.",
                show_alert=True,
            )
            return
    finally:
        db.close()

    await state.update_data(deliver_order_id=order_id)
    await state.set_state(DeliverOrder.content)

    destination_note = ""
    db = SessionLocal()
    try:
        order = db.get(Order, order_id)
        is_api_order = bool(order and db.query(ApiOrder.id).filter(ApiOrder.order_id == order.id).first())
        if order and order.delivery_telegram_id:
            destination_note = (
                "\n\n📨 This is a reseller-customer order. It will be sent by the "
                "separate Delivery Bot to the supplied customer Telegram ID."
            )
        elif order and is_api_order:
            destination_note = (
                "\n\n👤 No customer Telegram ID was supplied. The Delivery Bot will "
                "send the completed details only to you, so you can forward them yourself."
            )
        elif order and order.delivery_type in {"manual", "hybrid"}:
            destination_note = (
                "\n\n📨 The Delivery Bot will send the details to the buyer. If they have not "
                "started it, the order will wait until they send /start."
            )
    finally:
        db.close()

    if order and order.manual_pending_quantity:
        destination_note += f"\n\nDeliver remaining units only: {order.manual_pending_quantity}. Automatic: {order.automatic_quantity}."
    await answer_card(callback,
        "📤 Send the content to deliver to the buyer "
        "(account, key, or any message).\n\n"
        "It will be sent to them exactly as you type it, "
        "and the order will be marked completed."
        + destination_note
    )
    await callback.answer()


@retry_on_write_conflict(max_attempts=3)
def _do_deliver(order_id: int, delivered_text: str, admin_id: int) -> dict:
    with transaction() as db:
        order = (
            db.query(Order)
            .filter(Order.id == order_id)
            .with_for_update()
            .first()
        )

        if not order:
            return {"error": "Order not found."}
        if order.refunded:
            return {"error": "This order was already refunded, not delivering it."}
        if order.status == "deleted":
            return {"error": "This order was deleted."}
        if order.status == "completed":
            return {"error": "This order has already been delivered."}

        if order.status not in {"pending_manual", "preorder", "processing"}:
            return {"error": "This order is not ready for manual delivery."}
        if order.assigned_admin_id not in (None, admin_id):
            return {"error": "This order belongs to another admin. Reassign it in the team workspace first."}
        if not delivered_text or not delivered_text.strip():
            return {"error": "Delivery content cannot be empty."}
        order.assigned_admin_id = admin_id
        order.assigned_at = order.assigned_at or datetime.utcnow()
        record_event(db, order.id, "completed", "Team delivery completed.", actor_id=admin_id)
        was_preorder = order.is_preorder

        order.delivered_account = "\n".join(filter(None, [order.delivered_account, delivered_text]))
        order.manual_pending_quantity = 0
        order.status = "completed"
        start_coverage(order)
        order.is_preorder = False
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id), action="manual_deliver_order",
            entity_type="order", entity_id=str(order.id), summary="Order manually marked delivered.",
        ))

        # Referral commission for a preorder is deliberately deferred
        # until it's actually fulfilled (see handlers/products.py) —
        # pay it now, exactly once, on that transition. A
        # non-preorder pending_manual order already had its
        # commission paid at purchase time, so it must NOT be paid
        # again here.
        commission_paid = None
        if was_preorder:
            buyer = (
                db.query(User)
                .filter(User.telegram_id == order.telegram_id)
                .with_for_update()
                .first()
            )
            if buyer and buyer.referred_by:
                referrer = (
                    db.query(User)
                    .filter(User.telegram_id == buyer.referred_by)
                    .with_for_update()
                    .first()
                )
                if referrer is not None:
                    commission = _money(_money(order.amount) * REFERRAL_COMMISSION_RATE)
                    if commission > 0:
                        referrer.referral_earnings = _money(referrer.referral_earnings) + commission
                        if REFERRAL_CREDIT_TO_BALANCE:
                            referrer.balance = _money(referrer.balance) + commission
                        commission_paid = {
                            "referrer_telegram_id": referrer.telegram_id,
                            "amount": commission,
                        }

        logger.info("Order %s delivered manually (was_preorder=%s)", order.id, was_preorder)

        return {
            "order_id": order.id,
            "buyer_id": order.telegram_id,
            "delivery_telegram_id": order.delivery_telegram_id,
            "is_api_order": bool(
                db.query(ApiOrder.id).filter(ApiOrder.order_id == order.id).first()
            ),
            "product_id": order.product_id,
            "product_name": order.product_name,
            "delivered_text": delivered_text,
            "commission_paid": commission_paid,
        }



async def _dispatch_blocking(func, *args):
    """Small shared helper so both this module's handlers and
    products.py's use the same off-event-loop dispatch pattern."""
    return await asyncio.to_thread(func, *args)


@router.message(DeliverOrder.content)
async def deliver_order_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    order_id = data.get("deliver_order_id")
    await state.clear()

    if not order_id:
        return

    try:
        result = await _dispatch_blocking(_do_deliver, order_id, message.text, message.from_user.id)
    except SQLAlchemyError:
        logger.exception("Database error delivering order %s", order_id)
        await answer_card(message, "❌ Database error while delivering. Please try again.")
        return
    except Exception:
        logger.exception("Unexpected error delivering order %s", order_id)
        await answer_card(message, "❌ Unexpected error while delivering.")
        return

    if "error" in result:
        await answer_card(message, f"❌ {result['error']}")
        return

    try:
        # Query the full order and product for the rich notification
        db2 = SessionLocal()
        try:
            full_order = db2.query(Order).filter(Order.id == order_id).first()
            product = db2.query(Product).filter(Product.id == result["product_id"]).first()
            has_instruction = bool(product and product.delivery_instruction)
            product_id_for_callback = result["product_id"]
        finally:
            db2.close()

        # Build the formatted order message (same style as order_detail view)
        status_config = {
            "completed": {"emoji": "✅", "label": "Completed", "icon": "🟢", "progress": 100},
        }.get(full_order.status, {"emoji": "✅", "label": "Completed", "icon": "🟢", "progress": 100})

        order_text = (
            "✅ <b>ORDER DELIVERED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🧾 <b>Order:</b> <code>#{full_order.id}</code>\n"
            f"📦 <b>Product:</b> {escape(str(full_order.product_name or 'Product'))}\n"
            f"🔢 <b>Quantity:</b> {full_order.quantity or 1}\n"
            f"💰 <b>Total:</b> ${float(full_order.amount):.2f}\n"
        )

        if full_order.delivery_type:
            delivery_labels = {
                "automatic": "🤖 Auto-Delivery",
                "manual": "👨‍💼 Manual Delivery",
                "hybrid": "🔀 Hybrid",
            }
            dt_label = delivery_labels.get(full_order.delivery_type, full_order.delivery_type)
            order_text += f"🚚 <b>Delivery:</b> {dt_label}\n"

        order_text += (
            "\n🔑 <b>Your delivery details:</b>\n"
            f"<pre>{escape(str(result['delivered_text']))}</pre>\n\n"
            "🎉 Enjoy your purchase!"
        )

        # Build keyboard with buttons for ordinary main-bot customers only.
        keyboard_buttons = []
        if has_instruction:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="📋 📖 Delivery Instructions",
                    callback_data=f"delivery_instruction_{product_id_for_callback}",
                    style="primary"
                )
            ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="⭐ Rate This Order", callback_data=f"order_rate_{order_id}", style="success"),
            InlineKeyboardButton(text="📎 Receipt", callback_data=f"order_receipt_{order_id}", style="primary")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="🔗 Share Order", callback_data=f"order_share_{order_id}", style="primary"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data=f"order_detail_{order_id}", style="primary")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="📋 All Orders", callback_data="orders_menu", style="primary"),
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")
        ])

        uses_delivery_bot = full_order.delivery_type in {"manual", "hybrid"}
        if uses_delivery_bot:
            # Normal shop orders go to their buyer. API orders go to the
            # explicitly supplied end customer, or only to the fulfilling
            # admin when no customer Telegram ID was supplied.
            destination_id = (
                result["delivery_telegram_id"]
                or (message.from_user.id if result["is_api_order"] else result["buyer_id"])
            )
            recipient_label = (
                "the customer" if result["delivery_telegram_id"] or not result["is_api_order"]
                else "you (admin delivery copy)"
            )
            try:
                delivery_result = await send_manual_delivery(order_id, destination_id, message.from_user.id)
            except DeliveryBridgeError as exc:
                await answer_card(message,
                    f"⚠️ Order was marked delivered, but the Delivery Bot could not confirm delivery: {exc}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🔁 Check / retry delivery", callback_data=f"retry_api_delivery_{order_id}")
                    ]]),
                )
                return
            waiting = delivery_result.get("status") == "waiting_for_start"
            if waiting and not result["is_api_order"] and config.DELIVERY_BOT_USERNAME:
                try:
                    await queue_message(message.bot, f"delivery-start:{order_id}",
                        destination_id,
                        "📦 Your order is ready. Start the Delivery Bot to receive it here:\n"
                        f"https://t.me/{escape(config.DELIVERY_BOT_USERNAME)}?start=order_{order_id}",
                    )
                except Exception:
                    logger.info("Could not send delivery-bot start link to buyer %s", destination_id)
            status_text = (
                f"⏳ Delivery is queued for {recipient_label}. The Delivery Bot will send it "
                "automatically after the recipient opens it and sends /start."
                if waiting else
                f"✅ Manual delivery sent through the Delivery Bot to {recipient_label}."
            )
            await answer_card(message,
                status_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔁 Check delivery status", callback_data=f"retry_api_delivery_{order_id}")
                ]]),
            )
        else:
            destination_id = result["buyer_id"]
            await queue_message(message.bot, f"manual-delivery:{order_id}",
                destination_id,
                order_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )
    except Exception:
        logger.exception("Failed to notify delivery recipient %s", result.get("delivery_telegram_id") or result["buyer_id"])
        await answer_card(message,
            "⚠️ Order marked delivered, but I couldn't message the "
            "recipient directly. They must start the correct bot before delivery."
        )
        return


@router.callback_query(F.data.startswith("retry_api_delivery_"))
async def retry_api_manual_delivery(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        order_id = int(callback.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid order.", show_alert=True)
        return

    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        is_api_order = bool(order and db.query(ApiOrder.id).filter(ApiOrder.order_id == order_id).first())
        if not order or order.delivery_type not in {"manual", "hybrid"} or order.status != "completed" or order.refunded or not order.delivered_account:
            await callback.answer("This order is not eligible for delivery retry.", show_alert=True)
            return
        recipient_id = int(order.delivery_telegram_id or (callback.from_user.id if is_api_order else order.telegram_id))
    finally:
        db.close()

    try:
        result = await send_manual_delivery(order_id, recipient_id, callback.from_user.id)
    except DeliveryBridgeError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    await callback.answer(
        "Already sent; no duplicate was sent." if result.get("status") == "already_sent" else
        "Queued. It will be sent after the recipient starts the Delivery Bot." if result.get("status") == "waiting_for_start" else
        "Delivery sent.",
        show_alert=True,
    )
