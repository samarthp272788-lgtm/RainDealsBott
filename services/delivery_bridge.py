"""Calls the Cloudflare delivery-bot Worker without exposing its bot token."""

from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import config


class DeliveryBridgeError(RuntimeError):
    pass


def _post_delivery(order_id: int, recipient_id: int, admin_id: int) -> dict:
    if not config.DELIVERY_BRIDGE_URL or not config.DELIVERY_BRIDGE_SECRET:
        raise DeliveryBridgeError(
            "Cloudflare delivery bridge is not configured. Set DELIVERY_BRIDGE_URL and DELIVERY_BRIDGE_SECRET."
        )

    body = json.dumps({
        "order_id": int(order_id),
        "recipient_id": int(recipient_id),
        "admin_id": int(admin_id),
    }).encode("utf-8")
    request = Request(
        f"{config.DELIVERY_BRIDGE_URL}/internal/delivery",
        data=body,
        headers={
            "content-type": "application/json",
            "x-delivery-bridge-secret": config.DELIVERY_BRIDGE_SECRET,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("detail", {}).get("message") or detail.get("message")
        except Exception:
            message = None
        raise DeliveryBridgeError(message or f"Delivery Worker rejected the request (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DeliveryBridgeError(
            "Could not reach the delivery Worker. Check its deployment and try the outbox retry."
        ) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise DeliveryBridgeError("Delivery Worker returned an invalid response.") from exc

    if not result.get("success"):
        raise DeliveryBridgeError(result.get("message") or "Delivery Worker could not send this order.")
    # A successful HTTP response alone is not enough to complete the order:
    # require a durable/known delivery state so malformed or partial Worker
    # responses leave the order retryable as delivery_pending.
    status = str(result.get("status") or "").strip().lower()
    if status not in {"sent", "queued", "waiting_for_start", "already_sent"}:
        raise DeliveryBridgeError("Delivery Worker did not return a confirmed delivery state.")
    return result


async def send_manual_delivery(order_id: int, recipient_id: int, admin_id: int) -> dict:
    return await asyncio.to_thread(_post_delivery, order_id, recipient_id, admin_id)
