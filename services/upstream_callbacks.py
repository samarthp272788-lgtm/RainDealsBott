from services.warranty import start_coverage
from services.order_workflow import record_event
from services.notification_outbox import enqueue
import hashlib
import hmac
import json
import time
from html import escape
from decimal import Decimal, ROUND_DOWN
import logging

from database import SessionLocal
from models.order import Order
from models.provider import Provider
from models.user import User

logger = logging.getLogger(__name__)


def _delivery_payload(fulfillment):
    if isinstance(fulfillment, (str, list, int, float)) and not isinstance(fulfillment, bool):
        delivery = fulfillment
    elif isinstance(fulfillment, dict):
        delivery = None
        for key in ("payload", "delivery_data", "delivery", "delivery_items",
                    "accounts", "credentials", "code", "result"):
            if fulfillment.get(key) is not None:
                delivery = fulfillment[key]
                break
    else:
        return None
    if delivery is None or delivery == "" or delivery == [] or delivery == {}:
        return None
    if isinstance(delivery, str):
        return delivery.strip() or None
    if isinstance(delivery, (int, float)) and not isinstance(delivery, bool):
        return str(delivery)
    if isinstance(delivery, list):
        values = [
            json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item).strip()
            for item in delivery
            if item is not None and str(item).strip()
        ]
        return "\n".join(values) if values else None
    if isinstance(delivery, dict) and not any(
        value is not None and str(value).strip() for value in delivery.values()
    ):
        return None
    return json.dumps(delivery, ensure_ascii=False)


def _delivery_unit_count(fulfillment, delivery) -> int:
    """Prefer provider item arrays (one entry per unit) over counting lines."""
    if isinstance(fulfillment, dict):
        for key in ("payload", "delivery_data", "delivery", "delivery_items",
                    "accounts", "credentials", "code", "result"):
            value = fulfillment.get(key)
            if isinstance(value, list):
                return len([item for item in value if item is not None and str(item).strip()])
    if isinstance(fulfillment, list):
        return len([item for item in fulfillment if item is not None and str(item).strip()])
    return len([line for line in str(delivery or "").splitlines() if line.strip()])


def _refund_supplier_order(session, order):
    user = session.query(User).filter(User.telegram_id == order.telegram_id).with_for_update().first()
    if not user:
        raise RuntimeError(f"Buyer record missing while refunding supplier order #{order.id}")
    user.balance = Decimal(str(user.balance or 0)) + Decimal(str(order.amount or 0))
    user.total_orders = max(0, int(user.total_orders or 0) - 1)
    user.total_spent = max(Decimal("0"), Decimal(str(user.total_spent or 0)) - Decimal(str(order.amount or 0)))

    if user.referred_by:
        referrer = session.query(User).filter(User.telegram_id == user.referred_by).with_for_update().first()
        if referrer:
            commission = getattr(order, "referral_commission_amount", None)
            if commission is None:
                from config import REFERRAL_COMMISSION_RATE
                commission = (Decimal(str(order.amount or 0)) * Decimal(str(REFERRAL_COMMISSION_RATE))).quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN
                )
            commission = Decimal(str(commission or 0))
            referrer.referral_earnings = max(
                Decimal("0"), Decimal(str(referrer.referral_earnings or 0)) - commission
            )
            credited = getattr(order, "referral_commission_to_balance", None)
            if credited is None:
                from config import REFERRAL_CREDIT_TO_BALANCE
                credited = REFERRAL_CREDIT_TO_BALANCE
            if credited:
                referrer.balance = Decimal(str(referrer.balance or 0)) - commission


def _callback_error(message: str) -> dict:
    return {"ok": False, "message": message}


async def process_upstream_callback(raw_body: bytes, headers, bot) -> dict:
    api_key = headers.get("Dujiao-Next-Api-Key", "")
    timestamp = headers.get("Dujiao-Next-Timestamp", "")
    provided_signature = headers.get("Dujiao-Next-Signature", "")
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError):
        return _callback_error("invalid_timestamp")

    if abs(int(time.time()) - timestamp_value) > 60:
        return _callback_error("timestamp_expired")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _callback_error("invalid_json")

    if not isinstance(payload, dict):
        return _callback_error("invalid_payload")

    session = SessionLocal()
    notification = None
    try:
        provider = session.query(Provider).filter(Provider.api_key == api_key, Provider.is_active == True).first()
        if not provider or not provider.api_secret:
            return _callback_error("invalid_api_key")

        body_md5 = hashlib.md5(raw_body).hexdigest()
        sign_string = f"POST\n/api/v1/upstream/callback\n{timestamp}\n{body_md5}"
        expected_signature = hmac.new(
            provider.api_secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, provided_signature):
            return _callback_error("invalid_signature")

        downstream_order_no = payload.get("downstream_order_no")
        if not downstream_order_no:
            return _callback_error("missing_downstream_order_no")

        order = (
            session.query(Order)
            .filter(
                Order.reseller_order_id == str(downstream_order_no),
                Order.reseller_id == provider.id,
            )
            .with_for_update()
            .first()
        )
        if not order:
            return _callback_error("order_not_found")

        if order.status in {"completed", "refunded"}:
            return {"ok": True, "message": "already_processed"}

        status = str(payload.get("status") or "").strip().lower().replace(" ", "_")
        fulfillment = payload.get("fulfillment")
        delivery = _delivery_payload(fulfillment)

        if status in {"delivered", "completed"}:
            if delivery:
                delivered_units = _delivery_unit_count(fulfillment, delivery)
                expected_units = max(1, int(order.quantity or 1))
                if expected_units > 1 and delivered_units < expected_units:
                    logger.warning(
                        "Supplier callback has incomplete delivery order_id=%s expected=%s got=%s",
                        order.id, expected_units, delivered_units,
                    )
                    order.delivered_account = str(delivery)
                    order.automatic_quantity = delivered_units
                    order.manual_pending_quantity = expected_units - delivered_units
                    order.delivery_type = "hybrid"
                    order.status = "pending_manual"
                    record_event(
                        session, order.id, "supplier_partial",
                        f"Supplier delivered {delivered_units} of {expected_units} units; the team will deliver the remainder.",
                    )
                    from services.notification_outbox import enqueue_admin
                    enqueue_admin(
                        session, f"supplier-incomplete:{order.id}",
                        f"Supplier order #{order.id} reported {delivered_units} delivery line(s) for {expected_units} purchased unit(s). Review with the provider before completing it.",
                    )
                    enqueue(
                        session, f"upstream:{order.id}:partial:{delivered_units}", order.telegram_id,
                        f"📦 <b>PARTIAL ORDER DELIVERY</b>\n\n"
                        f"Product: {escape(order.product_name)}\n"
                        f"Order: <code>#{order.id}</code>\n"
                        f"Delivered: {delivered_units} of {expected_units}\n"
                        f"Team delivery pending: {expected_units - delivered_units}\n\n"
                        f"<code>{escape(str(delivery))}</code>\n\n"
                        "Open My Orders to track the remaining delivery.",
                        source_order_id=order.id,
                    )
                    session.commit()
                    return {"ok": True, "message": "partial_delivery_needs_review"}
                order.delivered_account = str(delivery)
                order.automatic_quantity = expected_units
                order.manual_pending_quantity = 0
                order.status = "completed"
                start_coverage(order)
                record_event(session, order.id, "completed", "Supplier delivery completed.")
                notification = (order.telegram_id, order.product_name, str(delivery), "delivered")
            else:
                logger.warning("Supplier reported delivery without content order_id=%s", order.id)
                order.delivery_type = "hybrid"
                order.manual_pending_quantity = max(
                    0, int(order.quantity or 1) - int(order.automatic_quantity or 0)
                )
                from services.notification_outbox import enqueue_admin
                enqueue_admin(
                    session, f"supplier-missing-delivery:{order.id}",
                    f"Supplier order #{order.id} was marked delivered without delivery content. Review it with the provider.",
                )
                session.commit()
                return {"ok": True, "message": "delivery_content_missing"}
        elif status in {"canceled", "cancelled", "failed", "error", "rejected"}:
            if order.delivered_account or int(order.automatic_quantity or 0) > 0:
                logger.warning(
                    "Supplier failed after partial delivery; withholding automatic refund order_id=%s",
                    order.id,
                )
                order.status = "pending_manual"
                order.delivery_type = "hybrid"
                order.manual_pending_quantity = max(
                    0, int(order.quantity or 1) - int(order.automatic_quantity or 0)
                )
                from services.notification_outbox import enqueue_admin
                enqueue_admin(
                    session, f"supplier-failed-after-partial:{order.id}",
                    f"Supplier marked order #{order.id} failed after some delivery content was recorded. No automatic refund was issued. Review supplier status and decide whether to complete or refund it.",
                )
                record_event(session, order.id, "supplier_review", "Supplier failed after partial delivery; awaiting team review.")
                session.commit()
                return {"ok": True, "message": "partial_delivery_requires_review"}
            if not order.refunded:
                _refund_supplier_order(session, order)
                order.refunded = True
                order.refund_amount = order.amount
            order.status = "refunded"
            record_event(session, order.id, "refunded", "Supplier cancellation refunded to wallet.")
            notification = (order.telegram_id, order.product_name, "", "canceled")
        elif status in {"", "pending", "processing", "created", "queued", "in_progress", "accepted"}:
            order.status = "processing"
        else:
            logger.warning("Unsupported supplier callback status=%s order_id=%s; leaving order processing", status, order.id)
            order.status = "processing"

        if notification:
            telegram_id, product_name, delivery, outcome = notification
            if outcome == "delivered":
                message = (
                    f"✅ <b>Order delivered</b>\n\n"
                    f"Product: {escape(product_name)}\n"
                    f"Order: <code>{escape(str(downstream_order_no))}</code>\n\n"
                    f"<code>{escape(delivery)}</code>"
                )
            else:
                message = (
                    f"❌ <b>Supplier canceled your order</b>\n\n"
                    f"Product: {escape(product_name)}\n"
                    f"Order: <code>{escape(str(downstream_order_no))}</code>\n"
                    "The amount has been returned to your bot balance."
                )
            enqueue(session, f'upstream:{order.id}:{outcome}', telegram_id, message)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


    return {"ok": True, "message": "received"}
