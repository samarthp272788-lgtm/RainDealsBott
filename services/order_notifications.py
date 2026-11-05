"""Durable notifications for the dedicated admin-order Telegram group."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import escape

import config
from sqlalchemy import text

from database import SessionLocal, engine, transaction
from services.notification_outbox import enqueue
from models.order import Order


logger = logging.getLogger(__name__)


def _prepare_schema() -> None:
    """Add the tracking column once and mark old orders as already seen."""
    with engine.begin() as connection:
        exists = connection.execute(
            text(
                "SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'orders' "
                "AND COLUMN_NAME = 'admin_notification_sent_at'"
            )
        ).first()
        if exists:
            return

        connection.execute(
            text(
                "ALTER TABLE orders "
                "ADD COLUMN admin_notification_sent_at DATETIME NULL"
            )
        )
        # Do not flood the group with historical orders on its first start.
        connection.execute(
            text(
                "UPDATE orders SET admin_notification_sent_at = UTC_TIMESTAMP() "
                "WHERE admin_notification_sent_at IS NULL"
            )
        )
        logger.info("Enabled persistent admin-order notification tracking")


def _pending_orders(limit: int = 50) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(Order)
            .filter(Order.admin_notification_sent_at.is_(None), Order.admin_notification_queued_at.is_(None))
            .order_by(Order.id.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": row.id,
                "telegram_id": row.telegram_id,
                "product_name": row.product_name,
                "quantity": row.quantity,
                "amount": row.amount,
                "status": row.status,
                "delivery_type": row.delivery_type,
                "is_preorder": row.is_preorder,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    finally:
        db.close()


def _queue_order(order, channel_id, bot_id):
    with transaction() as db:
        stored = db.query(Order).filter(Order.id == order['id']).with_for_update().first()
        if not stored or stored.admin_notification_sent_at or stored.admin_notification_queued_at:
            return
        enqueue(db, f"admin-order:{order['id']}", channel_id, _format_order(order), bot_id,
                source_order_id=order['id'])
        stored.admin_notification_queued_at = datetime.utcnow()


def _format_order(order: dict) -> str:
    status = str(order["status"] or "received").replace("_", " ").title()
    delivery = str(order["delivery_type"] or "manual").title()
    created = order["created_at"].strftime("%d-%b-%Y %I:%M %p") if order["created_at"] else "Unknown"
    action = ""
    if order["status"] in ("pending_manual", "preorder"):
        action = (
            "\n\n📋 <b>Action Required:</b>\n"
            f"Admin → Orders → #{order['id']} → Deliver"
        )
    return (
        "🛎 <b>ADMIN ORDER ALERT</b>\n\n"
        f"🆔 <b>Order:</b> <code>#{order['id']}</code>\n"
        f"👤 <b>Buyer ID:</b> <code>{order['telegram_id']}</code>\n"
        f"📦 <b>Product:</b> {escape(str(order['product_name']))}\n"
        f"🔢 <b>Quantity:</b> {order['quantity']}x\n"
        f"💰 <b>Total:</b> ${float(order['amount']):.2f}\n"
        f"🚚 <b>Delivery:</b> {escape(delivery)}\n"
        f"📊 <b>Status:</b> {escape(status)}\n"
        f"🕒 <b>Created:</b> {created}"
        f"{action}"
    )


async def order_notification_loop(bot) -> None:
    """Read unsent orders from the database and notify the dedicated group."""
    channel_id = getattr(config, "ADMIN_ORDER_NOTIFICATION_CHANNEL_ID", "").strip()
    if not channel_id:
        logger.warning("Admin order notifications are disabled: no group ID configured")
        return

    try:
        await asyncio.to_thread(_prepare_schema)
    except Exception:
        logger.exception("Could not prepare admin-order notification tracking")
        return

    interval = getattr(config, "ADMIN_ORDER_NOTIFICATION_POLL_SECONDS", 5)
    while True:
        try:
            orders = await asyncio.to_thread(_pending_orders)
            for order in orders:
                await asyncio.to_thread(_queue_order, order, channel_id, bot.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to send an admin order notification")
        await asyncio.sleep(interval)
