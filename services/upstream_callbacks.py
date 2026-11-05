from services.warranty import start_coverage
from services.order_workflow import record_event
from services.notification_outbox import enqueue
import hashlib
import hmac
import json
import time
from html import escape

from database import SessionLocal
from models.order import Order
from models.provider import Provider
from models.user import User


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

        status = str(payload.get("status") or "").lower()
        fulfillment = payload.get("fulfillment")
        if not isinstance(fulfillment, dict):
            fulfillment = {}
        delivery = fulfillment.get("payload")
        if delivery is None and fulfillment.get("delivery_data") is not None:
            delivery = json.dumps(fulfillment["delivery_data"], ensure_ascii=False)

        if status in {"delivered", "completed"}:
            if delivery:
                order.delivered_account = str(delivery)
                order.status = "completed"
                start_coverage(order)
                record_event(session, order.id, "completed", "Supplier delivery completed.")
                notification = (order.telegram_id, order.product_name, str(delivery), "delivered")
        elif status == "canceled":
            if not order.refunded:
                user = session.query(User).filter(User.telegram_id == order.telegram_id).with_for_update().first()
                if user:
                    user.balance = user.balance + order.amount
                    user.total_orders = max(0, user.total_orders - 1)
                    user.total_spent = max(0, user.total_spent - order.amount)
                order.refunded = True
            order.status = "refunded"
            record_event(session, order.id, "refunded", "Supplier cancellation refunded to wallet.")
            notification = (order.telegram_id, order.product_name, "", "canceled")
        else:
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
