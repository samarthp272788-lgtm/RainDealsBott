"""Recover delayed HMAC supplier orders when a provider callback is missed."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from database import SessionLocal
from models.order import Order

logger = logging.getLogger(__name__)


def _pending_orders(limit: int = 25, cursor_after: int | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(
            Order.id, Order.reseller_id, Order.reseller_order_id,
            Order.reseller_provider_order_id,
        ).filter(
                Order.status == "processing",
                Order.refunded.is_(False),
                Order.reseller_id.is_not(None),
                Order.reseller_order_id.is_not(None),
                Order.reseller_provider_order_id.is_not(None),
            )
        if cursor_after is not None:
            query = query.filter(Order.id > cursor_after)
        rows = query.order_by(Order.id.asc()).limit(limit).all()
        return [dict(order_id=row[0], provider_id=row[1], downstream_order_no=row[2],
                     provider_order_id=row[3]) for row in rows]
    finally:
        db.close()


async def _reconcile_one(order_ref: dict) -> None:
    from handlers.products import ResellerManager, _get_reseller_credentials
    from services.upstream_callbacks import process_upstream_callback

    if not ResellerManager:
        return
    credentials = await asyncio.to_thread(_get_reseller_credentials, str(order_ref["provider_id"]))
    if not credentials.get("api_key") or not credentials.get("api_secret"):
        return
    manager = ResellerManager(
        api_key=credentials["api_key"],
        base_url=credentials["base_url"],
        provider_config=credentials,
    )
    if not getattr(manager, "is_hmac_auth", False):
        return

    details = await asyncio.wait_for(
        manager.get_order(str(order_ref["provider_order_id"])), timeout=25
    )
    if not isinstance(details, dict):
        return
    nested = details.get("order") or details.get("data")
    if isinstance(nested, dict):
        details = {**details, **nested}
    status = str(details.get("status") or "").strip()
    if not status:
        return
    fulfillment = details.get("fulfillment")
    if isinstance(fulfillment, (str, list, int, float)) and not isinstance(fulfillment, bool):
        fulfillment = {"payload": fulfillment}
    elif not isinstance(fulfillment, dict):
        fulfillment = {}
        for key in ("delivery", "delivery_items", "delivery_data", "payload", "accounts", "credentials", "code", "result", "data"):
            if details.get(key) is not None:
                fulfillment["payload"] = details[key]
                break

    payload = {
        "downstream_order_no": str(order_ref["downstream_order_no"]),
        "status": status,
        "fulfillment": fulfillment,
    }
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    digest = hashlib.md5(raw_body).hexdigest()
    sign_string = f"POST\n/api/v1/upstream/callback\n{timestamp}\n{digest}"
    signature = hmac.new(credentials["api_secret"].encode("utf-8"), sign_string.encode("utf-8"), hashlib.sha256).hexdigest()
    result = await process_upstream_callback(raw_body, {
        "Dujiao-Next-Api-Key": credentials["api_key"],
        "Dujiao-Next-Timestamp": timestamp,
        "Dujiao-Next-Signature": signature,
    }, bot=None)
    logger.info("Supplier order reconciled order_id=%s status=%s result=%s",
                order_ref["order_id"], status, result.get("message"))


async def supplier_reconciliation_loop(_bot=None) -> None:
    """Poll pending HMAC orders as a fallback; signed callbacks remain primary."""
    cursor_after = None
    semaphore = asyncio.Semaphore(4)
    while True:
        try:
            orders = await asyncio.to_thread(_pending_orders, 25, cursor_after)
            if not orders and cursor_after is not None:
                cursor_after = None
                orders = await asyncio.to_thread(_pending_orders, 25, None)
            if orders:
                cursor_after = orders[-1]["order_id"]

            async def reconcile_limited(order_ref):
                async with semaphore:
                    try:
                        await _reconcile_one(order_ref)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Supplier order reconciliation failed order_id=%s", order_ref["order_id"])

            if orders:
                await asyncio.gather(*(reconcile_limited(order_ref) for order_ref in orders))
            else:
                cursor_after = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not load pending supplier orders for reconciliation")
        await asyncio.sleep(60)
