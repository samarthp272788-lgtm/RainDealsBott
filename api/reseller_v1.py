"""
Public Reseller API.

Endpoints:

GET  /api/v1/me
GET  /api/v1/products
POST /api/v1/order
GET  /api/v1/orders
GET  /api/v1/order/{order_id}

Authentication:

Authorization: Bearer YOUR_API_KEY

or:

X-API-Key: YOUR_API_KEY

The API key identifies the Telegram account.

No internal Wasmer secret is required because this is the
public API exposed directly by FastAPI.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import threading
import time

from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text

from config import (
    API_INVALID_AUTH_LIMIT_PER_MINUTE,
    API_MAX_CONCURRENT_ORDERS,
    API_ORDER_LIMIT_PER_MINUTE,
    API_ORDER_RECONCILE_AFTER_SECONDS,
    API_RATE_LIMIT_PER_MINUTE,
    API_RATE_LIMIT_PER_SECOND,
    API_TRUSTED_PROXY_CIDRS,
)
from database import SessionLocal, transaction

from models.api_key import ApiKey, ApiOrder
from models.order import Order
from models.product import Product
from models.user import User

from services.checkout_actions import api_checkout_key
from services.fulfillment_policy import api_order_capacity
from services.input_forms import InputError, api_requirement_values

from services.api_keys import (
    ApiKeyConfigurationError,
    authenticate_api_key,
)

from handlers.products import (
    _active_custom_price,
    _do_purchase,
    _do_reseller_purchase,
    _money,
    _real_stock,
)


logger = logging.getLogger(__name__)


# ============================================================
# ROUTER
# ============================================================

router = APIRouter(
    prefix="/api/v1",
    tags=["Reseller API"],
)


# ============================================================
# CLIENT ORDER ID VALIDATION
# ============================================================

_client_order_id = re.compile(
    r"^[A-Za-z0-9._:-]{1,80}$"
)


# ============================================================
# RATE LIMITING
# ============================================================

_rate_windows: dict[int, deque[float]] = defaultdict(deque)

_minute_rate_windows: dict[int, deque[float]] = defaultdict(deque)

_order_windows: dict[int, deque[float]] = defaultdict(deque)

_invalid_auth_windows: dict[str, deque[float]] = defaultdict(deque)

_in_flight_orders: dict[int, int] = defaultdict(int)

_rate_lock = asyncio.Lock()
_last_rate_cleanup = 0.0
_RATE_CLEANUP_INTERVAL_SECONDS = 300
_last_db_bucket_cleanup = 0.0
_db_bucket_cleanup_lock = threading.Lock()
def _load_trusted_proxy_networks():
    networks = []
    for value in API_TRUSTED_PROXY_CIDRS:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.error("Ignoring invalid API_TRUSTED_PROXY_CIDRS entry")
    return tuple(networks)


_trusted_proxy_networks = _load_trusted_proxy_networks()


# ============================================================
# API PRINCIPAL
# ============================================================

@dataclass(frozen=True)
class ApiPrincipal:
    key_id: int
    telegram_id: int


class ApiOrderInProgress(RuntimeError):
    pass


class ApiKeyRevokedBeforeReservation(RuntimeError):
    pass


# ============================================================
# REQUEST MODEL
# ============================================================

class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_id: int = Field(
        description="Product ID returned by GET /api/v1/products",
        gt=0,
    )

    quantity: int = Field(
        default=1,
        ge=1,
        le=100,
    )

    client_order_id: str = Field(
        min_length=1,
        max_length=80,
        description="Unique client order ID used for safe retries.",
    )

    delivery_telegram_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Telegram user ID for manual/customer delivery "
            "when applicable."
        ),
    )

    buyer_info: dict[StrictStr, StrictStr] | None = Field(
        default=None,
        max_length=8,
        description=(
            "Required product fields, keyed by the exact labels returned in "
            "the product's required_info list. Email, phone, and text fields "
            "are supported; Telegram photo/contact fields must use the bot."
        ),
    )

    @field_validator("buyer_info")
    @classmethod
    def bound_buyer_info(cls, values):
        if values is None:
            return None
        total_bytes = 0
        for label, value in values.items():
            if not label.strip() or len(label) > 100:
                raise ValueError("buyer_info labels must contain 1–100 characters.")
            if len(value) > 3000:
                raise ValueError("Each buyer_info value must be at most 3,000 characters.")
            try:
                total_bytes += len(label.encode("utf-8")) + len(value.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ValueError("buyer_info must contain valid UTF-8 text.") from exc
        if total_bytes > 12_000:
            raise ValueError("Combined buyer_info values must not exceed 12,000 UTF-8 bytes.")
        return values


# ============================================================
# ERROR HELPER
# ============================================================

def _api_error(
    code: str,
    message: str,
    http_status: int,
    headers: dict[str, str] | None = None,
) -> HTTPException:

    return HTTPException(
        status_code=http_status,
        detail={
            "success": False,
            "error": code,
            "message": message,
        },
        headers=headers,
    )


# ============================================================
# RATE LIMIT
# ============================================================

async def _limit_key(key_id: int) -> None:

    now = time.monotonic()

    async with _rate_lock:

        _cleanup_rate_windows_locked(now)

        window = _rate_windows[key_id]

        minute_window = _minute_rate_windows[key_id]

        while window and now - window[0] >= 1:
            window.popleft()

        while minute_window and now - minute_window[0] >= 60:
            minute_window.popleft()

        limit = max(
            1,
            int(API_RATE_LIMIT_PER_SECOND),
        )

        if len(window) >= limit:

            retry_after = max(1, int(1 - (now - window[0])) + 1)

            raise _api_error(
                "rate_limited",
                f"Maximum {limit} requests per second per API key.",
                429,
                headers={"Retry-After": str(retry_after)},
            )

        minute_limit = max(1, int(API_RATE_LIMIT_PER_MINUTE))

        if len(minute_window) >= minute_limit:

            retry_after = max(1, int(60 - (now - minute_window[0])) + 1)

            raise _api_error(
                "rate_limited",
                f"Maximum {minute_limit} requests per minute per API key.",
                429,
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)

        minute_window.append(now)

    try:
        allowed, retry_after = await asyncio.to_thread(
            _consume_shared_rate_bucket,
            f"api-key:{key_id}",
            "request-minute",
            minute_limit,
            60,
        )
    except Exception:
        logger.exception("Shared reseller API rate limiter is unavailable")
        raise _api_error(
            "rate_limit_unavailable",
            "Request protection is temporarily unavailable. Retry shortly.",
            503,
            headers={"Retry-After": "2"},
        )
    if not allowed:
        raise _api_error(
            "rate_limited",
            f"Maximum {minute_limit} requests per minute per API key across service instances.",
            429,
            headers={"Retry-After": str(retry_after)},
        )


async def _limit_order(key_id: int) -> None:
    """Allow only a small number of purchases and one active purchase per key."""

    now = time.monotonic()

    async with _rate_lock:

        _cleanup_rate_windows_locked(now)

        window = _order_windows[key_id]

        while window and now - window[0] >= 60:
            window.popleft()

        limit = max(1, int(API_ORDER_LIMIT_PER_MINUTE))

        if len(window) >= limit:

            retry_after = max(1, int(60 - (now - window[0])) + 1)

            raise _api_error(
                "order_rate_limited",
                f"Maximum {limit} order requests per minute per API key.",
                429,
                headers={"Retry-After": str(retry_after)},
            )

        concurrent_limit = max(1, int(API_MAX_CONCURRENT_ORDERS))

        if _in_flight_orders[key_id] >= concurrent_limit:

            raise _api_error(
                "order_in_progress",
                "Another order request is already being processed for this API key.",
                429,
                headers={"Retry-After": "1"},
            )

        window.append(now)

        _in_flight_orders[key_id] += 1

    try:
        allowed, retry_after = await asyncio.to_thread(
            _consume_shared_rate_bucket,
            f"api-key:{key_id}",
            "order-minute",
            limit,
            60,
        )
    except Exception:
        await _release_order(key_id)
        logger.exception("Shared reseller API order limiter is unavailable")
        raise _api_error(
            "rate_limit_unavailable",
            "Order protection is temporarily unavailable. Retry shortly.",
            503,
            headers={"Retry-After": "2"},
        )
    if not allowed:
        await _release_order(key_id)
        raise _api_error(
            "order_rate_limited",
            f"Maximum {limit} order requests per minute per API key across service instances.",
            429,
            headers={"Retry-After": str(retry_after)},
        )


async def _release_order(key_id: int) -> None:
    """Release a purchase slot after the endpoint has completed."""

    async with _rate_lock:

        if _in_flight_orders[key_id] <= 1:
            _in_flight_orders.pop(key_id, None)
        else:
            _in_flight_orders[key_id] -= 1


def _client_address(request: Request) -> str:
    """Resolve X-Forwarded-For only when the direct peer is explicitly trusted."""
    peer = request.client.host if request.client else "unknown"
    if not _trusted_proxy_networks:
        return peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in network for network in _trusted_proxy_networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    # Proxies append addresses. Walk from the trusted edge toward the client,
    # stopping at the first untrusted address; never accept arbitrary XFF from
    # a direct client.
    for candidate in reversed(forwarded.split(",")):
        try:
            address = ipaddress.ip_address(candidate.strip())
        except ValueError:
            continue
        if any(address in network for network in _trusted_proxy_networks):
            continue
        return str(address)
    return peer


def _consume_shared_rate_bucket(subject: str, scope: str, limit: int, period: int) -> tuple[bool, int]:
    """Atomically count a fixed-window limit in the shared SQL database."""
    global _last_db_bucket_cleanup
    now_epoch = time.time()
    window = int(now_epoch // period)
    digest = hashlib.sha256(f"{scope}:{subject}:{window}".encode("utf-8")).hexdigest()
    expires_at = datetime.utcfromtimestamp((window + 2) * period)
    cleanup = False
    with _db_bucket_cleanup_lock:
        if time.monotonic() - _last_db_bucket_cleanup >= _RATE_CLEANUP_INTERVAL_SECONDS:
            _last_db_bucket_cleanup = time.monotonic()
            cleanup = True

    with transaction() as db:
        if cleanup:
            db.execute(text(
                "DELETE FROM api_rate_limit_buckets "
                "WHERE expires_at < UTC_TIMESTAMP() LIMIT 1000"
            ))
        db.execute(text(
            "INSERT INTO api_rate_limit_buckets (bucket_hash, request_count, expires_at) "
            "VALUES (:bucket_hash, 1, :expires_at) "
            "ON DUPLICATE KEY UPDATE request_count = LEAST(request_count + 1, :cap)"
        ), {"bucket_hash": digest, "expires_at": expires_at, "cap": limit + 1})
        count = db.execute(text(
            "SELECT request_count FROM api_rate_limit_buckets "
            "WHERE bucket_hash = :bucket_hash"
        ), {"bucket_hash": digest}).scalar_one()

    retry_after = max(1, int(((window + 1) * period) - now_epoch) + 1)
    return int(count) <= limit, retry_after


async def _reject_invalid_auth_flood(client_address: str) -> None:
    """Stop repeated bad credentials before another database lookup is made."""

    now = time.monotonic()

    async with _rate_lock:

        _cleanup_rate_windows_locked(now)

        window = _invalid_auth_windows.get(client_address)

        if not window:
            return

        while window and now - window[0] >= 60:
            window.popleft()

        if not window:
            _invalid_auth_windows.pop(client_address, None)
            return

        limit = max(1, int(API_INVALID_AUTH_LIMIT_PER_MINUTE))

        if len(window) >= limit:

            retry_after = max(1, int(60 - (now - window[0])) + 1)

            raise _api_error(
                "invalid_auth_rate_limited",
                "Too many invalid API-key attempts. Try again later.",
                429,
                headers={"Retry-After": str(retry_after)},
            )


async def _record_invalid_auth(client_address: str) -> None:
    """Remember a rejected authentication attempt for one minute."""

    async with _rate_lock:
        now = time.monotonic()
        _cleanup_rate_windows_locked(now)
        _invalid_auth_windows[client_address].append(now)


def _cleanup_rate_windows_locked(now: float) -> None:
    """Periodically drop expired keys so long-lived workers do not grow forever.

    Caller must hold ``_rate_lock``. These limits remain process-local; shared
    enforcement across multiple Render instances requires a shared limiter.
    """
    global _last_rate_cleanup
    if now - _last_rate_cleanup < _RATE_CLEANUP_INTERVAL_SECONDS:
        return
    _last_rate_cleanup = now
    for mapping, lifetime in (
        (_rate_windows, 1.0),
        (_minute_rate_windows, 60.0),
        (_order_windows, 60.0),
        (_invalid_auth_windows, 60.0),
    ):
        for key, window in list(mapping.items()):
            while window and now - window[0] >= lifetime:
                window.popleft()
            if not window:
                mapping.pop(key, None)


def _request_fingerprint(payload: OrderRequest) -> str:
    """Stable digest of every client-controlled field that changes an order."""
    canonical = json.dumps(
        {
            "service_id": payload.service_id,
            "quantity": payload.quantity,
            "delivery_telegram_id": payload.delivery_telegram_id,
            "buyer_info": payload.buyer_info,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
# AUTHENTICATION
# ============================================================

def _extract_api_key(
    authorization: str | None,
    x_api_key: str | None,
) -> str:

    # --------------------------------------------------------
    # Preferred:
    #
    # Authorization: Bearer API_KEY
    # --------------------------------------------------------

    if authorization:

        value = authorization.strip()

        if value.lower().startswith("bearer "):

            key = value[7:].strip()

            if key:
                return key

        # Also allow:
        #
        # Authorization: API_KEY
        #
        # for compatibility.
        if value:
            return value

    # --------------------------------------------------------
    # Compatibility:
    #
    # X-API-Key: API_KEY
    # --------------------------------------------------------

    if x_api_key:

        value = x_api_key.strip()

        if value:
            return value

    return ""


def _load_principal(
    raw_key: str,
) -> ApiPrincipal | None:

    key = authenticate_api_key(raw_key)

    if not key:
        return None

    db = SessionLocal()

    try:

        user = (
            db.query(User)
            .filter(
                User.telegram_id == key.telegram_id
            )
            .first()
        )

        if not user:
            return None

        if user.is_banned:
            return None

        return ApiPrincipal(
            key_id=key.id,
            telegram_id=key.telegram_id,
        )

    finally:

        db.close()


async def api_principal(
    request: Request,
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
    x_api_key: str | None = Header(
        default=None,
        alias="X-API-Key",
    ),
) -> ApiPrincipal:

    client_address = _client_address(request)

    await _reject_invalid_auth_flood(client_address)

    raw_key = _extract_api_key(
        authorization,
        x_api_key,
    )

    if not raw_key:

        await _record_invalid_auth(client_address)

        raise _api_error(
            "missing_api_key",
            "Provide your API key using Authorization: Bearer <API_KEY>.",
            401,
        )

    try:

        principal = await asyncio.to_thread(
            _load_principal,
            raw_key,
        )

    except ApiKeyConfigurationError:

        logger.exception(
            "API key service is not configured."
        )

        raise _api_error(
            "server_configuration",
            "The API key service is not configured.",
            503,
        )

    except Exception:

        logger.exception(
            "API authentication failed."
        )

        raise _api_error(
            "authentication_error",
            "Unable to authenticate the API key.",
            503,
        )

    if not principal:

        await _record_invalid_auth(client_address)

        raise _api_error(
            "invalid_api_key",
            "Missing, invalid, revoked, or blocked API key.",
            401,
        )

    await _limit_key(
        principal.key_id
    )

    return principal


async def api_order_principal(
    principal: ApiPrincipal = Depends(api_principal),
) -> AsyncIterator[ApiPrincipal]:
    """Apply purchase-specific limits and always release the active slot."""

    await _limit_order(principal.key_id)

    try:
        yield principal
    finally:
        await _release_order(principal.key_id)


# ============================================================
# MONEY
# ============================================================

def _decimal_text(
    value: Decimal | None,
) -> str:

    value = _money(
        value or Decimal("0")
    )

    return (
        format(value, "f")
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# ============================================================
# PRODUCT RESPONSE
# ============================================================

def _required_info_payload(product: Product) -> list[dict[str, str]]:
    raw = product.required_info if isinstance(product.required_info, list) else []
    output = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "text").strip().lower()
        label = str(item.get("label") or kind.title()).strip()
        if label:
            output.append({"type": kind, "label": label})
    return output

def _product_payload(
    product: Product,
    price: Decimal,
    stock: int,
) -> dict:

    return {
        "service_id": str(product.id),

        "name": product.name,

        "description": (
            product.description
            or ""
        ),

        "category": (
            product.category
            or "General"
        ),

        "price": _decimal_text(price),

        "currency": "USDT",

        "stock": stock,

        # The reseller API only accepts orders against available stock; it
        # never offers Telegram's manual-preorder path.
        "preorder": False,

        "delivery_type": (
            product.delivery_type
            or "automatic"
        ),

        "required_info": _required_info_payload(product),
    }


def _list_products(
    telegram_id: int,
) -> list[dict]:

    db = SessionLocal()

    try:

        products = (
            db.query(Product)
            .filter(
                Product.is_active == True,
                Product.api_enabled == True,
                # Zero-price products belong to the Telegram Freebies catalog,
                # not the public reseller API catalog.
                Product.price > 0,
            )
            .order_by(
                Product.id.asc()
            )
            .all()
        )

        data: list[dict] = []

        for product in products:

            custom_price = _active_custom_price(
                db,
                telegram_id,
                product.id,
            )

            if custom_price is not None:

                price = custom_price

            else:

                price = _money(
                    product.price
                )

            if (getattr(product, "source", "own") or "own") == "reseller":
                # Supplier stock must come from the live provider cache.
                stock = _real_stock(product)
            else:
                # Do not advertise Telegram-only manual-fallback capacity as
                # reseller API stock for automatic/hybrid products.
                stock = api_order_capacity(product)

            data.append(
                _product_payload(
                    product,
                    price,
                    stock,
                )
            )

        return data

    finally:

        db.close()


# ============================================================
# ACCOUNT
# ============================================================

def _account_details(
    telegram_id: int,
) -> dict | None:

    db = SessionLocal()

    try:

        user = (
            db.query(User)
            .filter(
                User.telegram_id == telegram_id
            )
            .first()
        )

        if not user:
            return None

        if user.is_banned:
            return None

        return {
            "telegram_id": user.telegram_id,

            "name": user.full_name,

            "wallet_balance": _decimal_text(
                user.balance
            ),

            "currency": "USDT",
        }

    finally:

        db.close()


# ============================================================
# ORDER RESPONSE
# ============================================================

def _order_payload(
    order: Order,
) -> dict:

    is_manual = (
        order.delivery_type == "manual"
    )

    # Never expose manual delivery contents
    # through the reseller API.

    delivered = []

    if not is_manual:

        delivered = [
            line
            for line in (
                order.delivered_account
                or ""
            ).splitlines()
            if line
        ]

    return {

        "order_id": str(
            order.id
        ),

        "service_id": (
            str(order.product_id)
            if order.product_id is not None
            else None
        ),

        "service": order.product_name,

        "quantity": order.quantity,
        "automatic_quantity": order.automatic_quantity,
        "manual_pending_quantity": order.manual_pending_quantity,

        "amount": _decimal_text(
            order.amount
        ),

        "currency": "USDT",

        "status": order.status,

        "delivery_type": (
            order.delivery_type
        ),

        "delivery_to_customer": bool(
            order.delivery_telegram_id
        ),

        "delivery_destination": (
            "customer_delivery_bot"
            if order.delivery_telegram_id

            else "admin_delivery_bot"
            if is_manual

            else "api_response"
        ),

        "delivery_status": (
            "sent_to_delivery_bot"
            if (
                is_manual
                and order.status == "completed"
            )

            else "awaiting_manual_delivery"
            if is_manual

            else None
        ),

        "delivered_products": delivered,

        "created_at": (
            order.created_at.isoformat()
            if order.created_at
            else None
        ),
    }


# ============================================================
# FIND ORDER
# ============================================================

def _find_api_order(
    api_key_id: int,
    order_id: int,
) -> Order | None:

    db = SessionLocal()

    try:

        row = (
            db.query(ApiOrder)
            .filter(
                ApiOrder.api_key_id == api_key_id,
                ApiOrder.order_id == order_id,
            )
            .first()
        )

        if not row:
            return None

        return db.get(
            Order,
            order_id,
        )

    finally:

        db.close()


# ============================================================
# LIST ORDERS
# ============================================================

def _list_api_orders(
    api_key_id: int,
    offset: int,
    limit: int,
) -> tuple[int, list[Order]]:

    db = SessionLocal()

    try:

        query = (
            db.query(Order)
            .join(
                ApiOrder,
                ApiOrder.order_id == Order.id,
            )
            .filter(
                ApiOrder.api_key_id == api_key_id
            )
        )

        total = query.count()

        orders = (
            query
            .order_by(
                Order.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        )

        return total, orders

    finally:

        db.close()


# ============================================================
# IDEMPOTENCY RESERVATION
# ============================================================

def _reserve_order(
    api_key_id: int,
    client_order_id: str,
    request_fingerprint: str,
) -> tuple[ApiOrder | None, bool]:

    db = SessionLocal()

    try:

        # Serialize reservations by API key so the one-order-at-a-time rule is
        # shared across Render instances, not just one Python process.
        key = db.query(ApiKey).filter(
            ApiKey.id == api_key_id,
            ApiKey.is_active == True,
        ).with_for_update().first()
        if not key:
            raise ApiKeyRevokedBeforeReservation

        existing = (
            db.query(ApiOrder)
            .filter(
                ApiOrder.api_key_id == api_key_id,
                ApiOrder.client_order_id
                == client_order_id,
            )
            .first()
        )

        if existing:

            return existing, False

        cutoff = datetime.utcnow() - timedelta(seconds=API_ORDER_RECONCILE_AFTER_SECONDS)
        other_processing = (
            db.query(ApiOrder)
            .filter(
                ApiOrder.api_key_id == api_key_id,
                ApiOrder.status == "processing",
            )
            .order_by(ApiOrder.id.asc())
            .with_for_update()
            .first()
        )
        if other_processing:
            last_update = other_processing.updated_at or other_processing.created_at
            if last_update and last_update <= cutoff:
                other_processing.status = "needs_review"
                other_processing.error_message = "Order outcome is being reconciled. Do not retry this purchase yet."
                from services.notification_outbox import enqueue_admin
                enqueue_admin(
                    db,
                    f"api-order-reconcile:{other_processing.id}",
                    f"Reseller API reservation #{other_processing.id} is stale and has no confirmed local result. "
                    "Check the provider and API Developer → Orders before retrying it.",
                )
                db.commit()
            raise ApiOrderInProgress

        row = ApiOrder(
            api_key_id=api_key_id,
            client_order_id=client_order_id,
            request_fingerprint=request_fingerprint,
            status="processing",
        )

        db.add(row)

        try:

            db.commit()

        except IntegrityError:

            db.rollback()

            row = (
                db.query(ApiOrder)
                .filter(
                    ApiOrder.api_key_id
                    == api_key_id,

                    ApiOrder.client_order_id
                    == client_order_id,
                )
                .first()
            )

            return row, False

        db.refresh(row)

        return row, True

    finally:

        db.close()


# ============================================================
# SET ORDER RESULT
# ============================================================

def _set_api_order_result(
    api_order_id: int,
    order_id: int | None,
    error_message: str | None = None,
    final_status: str | None = None,
) -> None:

    db = SessionLocal()

    try:

        row = db.get(
            ApiOrder,
            api_order_id,
        )

        if not row:
            return

        row.order_id = order_id

        row.status = final_status or ("completed" if order_id else "failed")

        row.error_message = (
            (error_message or "")[:500]
            or None
        )

        db.commit()

    finally:

        db.close()


def _reconcile_stale_api_order(api_order_id: int) -> dict:
    """Recover a committed order or stop an old reservation from being replayed."""
    from services.notification_outbox import enqueue_admin

    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=API_ORDER_RECONCILE_AFTER_SECONDS)
    with transaction() as db:
        row = db.query(ApiOrder).filter(ApiOrder.id == api_order_id).with_for_update().first()
        if not row:
            return {"status": "missing", "order_id": None}
        if row.status != "processing":
            return {"status": row.status, "order_id": row.order_id}

        checkout_key = api_checkout_key(row.api_key_id, row.client_order_id)
        order = db.query(Order).filter(Order.checkout_key == checkout_key).first()
        if order:
            row.order_id = order.id
            row.status = "completed"
            row.error_message = None
            return {"status": "completed", "order_id": order.id}

        last_update = row.updated_at or row.created_at
        if not last_update or last_update > cutoff:
            return {"status": "processing", "order_id": None}

        # A remote supplier may have accepted the order before the process
        # stopped. Never mark it retryable or issue another supplier request.
        row.status = "needs_review"
        row.error_message = "Order outcome is being reconciled. Do not retry this purchase yet."
        enqueue_admin(
            db,
            f"api-order-reconcile:{row.id}",
            f"Reseller API reservation #{row.id} remained processing for at least "
            f"{API_ORDER_RECONCILE_AFTER_SECONDS // 60} minutes without a local order. "
            "Check the provider and RainDeals API Developer → Orders before resolving it. "
            "Do not submit another provider purchase until the remote outcome is known.",
        )
        return {"status": "needs_review", "order_id": None}


async def _queue_api_order_review(api_order_id: int) -> None:
    """Record an idempotent admin alert without blocking the ASGI event loop."""
    from services.notification_outbox import enqueue_admin

    def _write():
        with transaction() as db:
            enqueue_admin(
                db,
                f"api-order-reconcile:{api_order_id}",
                f"Reseller API order reservation #{api_order_id} needs reconciliation. "
                "Check the supplier and API Developer → Orders before resolving it; do not repeat a possible supplier purchase.",
            )

    try:
        await asyncio.to_thread(_write)
    except Exception:
        logger.exception("Could not queue API order reconciliation alert reservation_id=%s", api_order_id)


# ============================================================
# PRODUCT SOURCE
# ============================================================

def _product_source(
    product_id: int,
) -> str | None:

    db = SessionLocal()

    try:

        product = db.get(
            Product,
            product_id,
        )

        if not product:
            return None

        if not product.is_active:
            return None

        # An admin can temporarily stop API sales during low stock while the
        # normal Telegram shop remains available.
        if not product.api_enabled:
            return None

        # Freebies are claimed from the Telegram Freebies section only. Do not
        # allow a public API client to bypass that catalog boundary.
        if _money(product.price or Decimal("0")) <= Decimal("0"):
            return None

        return (
            product.source
            or "own"
        )

    finally:

        db.close()


def _product_required_info(product_id: int) -> list | None:
    """Return buyer requirements for an active API-enabled product."""
    db = SessionLocal()
    try:
        product = db.get(Product, product_id)
        if not product or not product.is_active or not product.api_enabled:
            return None
        return product.required_info if isinstance(product.required_info, list) else []
    finally:
        db.close()


# ============================================================
# GET ACCOUNT
# ============================================================

@router.get("/me")
async def get_me(
    principal: ApiPrincipal = Depends(
        api_principal
    ),
):

    account = await asyncio.to_thread(
        _account_details,
        principal.telegram_id,
    )

    if not account:

        raise _api_error(
            "account_unavailable",
            "The API account is unavailable.",
            403,
        )

    return {
        "success": True,
        **account,
    }


# ============================================================
# GET PRODUCTS
# ============================================================

@router.get("/products")
async def get_products(
    principal: ApiPrincipal = Depends(
        api_principal
    ),
):

    products = await asyncio.to_thread(
        _list_products,
        principal.telegram_id,
    )

    return {
        "success": True,
        "services": products,
    }


# ============================================================
# CREATE ORDER
# ============================================================

@router.post(
    "/order",
    status_code=status.HTTP_200_OK,
)
async def create_order(
    payload: OrderRequest,
    principal: ApiPrincipal = Depends(api_order_principal),
):

    # --------------------------------------------------------
    # Validate client order ID
    # --------------------------------------------------------

    if not _client_order_id.fullmatch(
        payload.client_order_id
    ):

        raise _api_error(
            "invalid_client_order_id",
            (
                "Use only letters, numbers, "
                "dot, underscore, dash, or colon."
            ),
            400,
        )

    request_fingerprint = _request_fingerprint(payload)

    # Reject invalid buyer details before reserving the idempotency key. The
    # checkout transaction validates again against the locked product row.
    required_info = await asyncio.to_thread(
        _product_required_info,
        payload.service_id,
    )
    if required_info is not None:
        try:
            api_requirement_values(required_info, payload.buyer_info)
        except InputError as exc:
            raise _api_error("buyer_info_invalid", str(exc), 422)

    # --------------------------------------------------------
    # Reserve idempotency key
    # --------------------------------------------------------

    try:
        request_row, is_new = await asyncio.to_thread(
            _reserve_order,
            principal.key_id,
            payload.client_order_id,
            request_fingerprint,
        )
    except ApiOrderInProgress:
        raise _api_error(
            "order_in_progress",
            "Another order for this API key is still being processed. Retry after checking its client_order_id.",
            429,
            headers={"Retry-After": "3"},
        )
    except ApiKeyRevokedBeforeReservation:
        raise _api_error("invalid_api_key", "This API key was revoked before the purchase was reserved.", 401)

    if not request_row:

        raise _api_error(
            "order_reservation_failed",
            (
                "Could not reserve this order. "
                "Retry with the same client_order_id."
            ),
            503,
        )

    # --------------------------------------------------------
    # Existing request
    # --------------------------------------------------------

    if not is_new:
        if request_row.request_fingerprint != request_fingerprint:
            raise _api_error(
                "idempotency_conflict",
                "This client_order_id is already bound to a different purchase request. Use a new ID for a changed order.",
                409,
            )
        if not request_row.order_id and request_row.status == "processing":
            recovered = await asyncio.to_thread(_reconcile_stale_api_order, request_row.id)
            if recovered.get("order_id"):
                request_row.order_id = recovered["order_id"]
                request_row.status = "completed"
            else:
                request_row.status = recovered.get("status", request_row.status)


        if request_row.order_id:

            order = await asyncio.to_thread(
                _find_api_order,
                principal.key_id,
                request_row.order_id,
            )

            if order:

                return {
                    "success": True,
                    "idempotent_replay": True,
                    "order": _order_payload(
                        order
                    ),
                }

        if request_row.status == "needs_review":
            raise _api_error(
                "order_needs_review",
                "The previous order outcome is still being reconciled. Do not retry or create a replacement order until support confirms its status.",
                409,
            )

        if request_row.status == "processing":

            raise _api_error(
                "order_processing",
                (
                    "This client_order_id is already "
                    "being processed. Retry shortly "
                    "with the same ID."
                ),
                409,
            )

        raise _api_error(
            "previous_order_failed",
            "This client_order_id previously failed. Check the order status before using a new ID.",
            409,
        )

    # --------------------------------------------------------
    # Check product
    # --------------------------------------------------------

    source = await asyncio.to_thread(
        _product_source,
        payload.service_id,
    )

    if not source:

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            "Product not found or unavailable.",
        )

        raise _api_error(
            "product_unavailable",
            "Product not found or unavailable.",
            404,
        )

    # --------------------------------------------------------
    # Purchase
    # --------------------------------------------------------

    try:

        if source == "reseller":

            result = await _do_reseller_purchase(
                principal.telegram_id,
                payload.service_id,
                payload.quantity,
                checkout_key=api_checkout_key(principal.key_id, payload.client_order_id),
                buyer_info=payload.buyer_info,
                require_buyer_info=True,
                api_key_id=principal.key_id,
            )

        else:

            result = await asyncio.to_thread(
                _do_purchase,
                principal.telegram_id,
                payload.service_id,
                payload.quantity,
                payload.delivery_telegram_id,
                api_checkout_key(principal.key_id, payload.client_order_id),
                buyer_info=payload.buyer_info,
                require_buyer_info=True,
                allow_preorder=False,
                api_key_id=principal.key_id,
            )

    except Exception:

        logger.exception(
            "Order processing exception"
        )

        if source == "reseller":
            await asyncio.to_thread(
                _set_api_order_result,
                request_row.id,
                None,
                "Supplier outcome may be ambiguous. Support must reconcile it before another order is placed.",
                "needs_review",
            )
            await _queue_api_order_review(request_row.id)
            raise _api_error(
                "order_needs_review",
                "The supplier outcome could not be confirmed. Do not retry this purchase; support will reconcile its status.",
                409,
            )

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            "Order processing failed. No completed order was recorded.",
        )

        raise _api_error(
            "order_failed",
            "Order processing failed.",
            500,
        )

    # --------------------------------------------------------
    # Purchase returned error
    # --------------------------------------------------------

    if result and result.get('duplicate_order_id'):
        result['order_id'] = result['duplicate_order_id']

    if not result:

        message = "Order failed."

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            message,
        )

        raise _api_error(
            "order_failed",
            message,
            400,
        )

    if result.get("error"):

        message = str(
            result["error"]
        )

        if result.get("needs_review"):
            safe_message = "Supplier outcome may be ambiguous. Support must reconcile it before another order is placed."
            await asyncio.to_thread(
                _set_api_order_result,
                request_row.id,
                None,
                safe_message,
                "needs_review",
            )
            await _queue_api_order_review(request_row.id)
            raise _api_error(
                "order_needs_review",
                "The supplier outcome could not be confirmed. Do not retry this purchase; support will reconcile its status.",
                409,
            )

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            "Order failed. No completed order was recorded.",
        )

        if message == "insufficient_balance":

            raise _api_error(
                "insufficient_balance",
                "Insufficient wallet balance.",
                400,
            )

        if message == "api_key_revoked":
            raise _api_error(
                "invalid_api_key",
                "This API key was revoked before the purchase was dispatched.",
                401,
            )

        if message.startswith("buyer_info_invalid:"):
            raise _api_error(
                "buyer_info_invalid",
                message.partition(":")[2].strip(),
                422,
            )

        if message.startswith("out_of_stock:"):
            raise _api_error(
                "out_of_stock",
                message.partition(":")[2].strip(),
                409,
            )

        if message.startswith("product_unavailable:"):
            raise _api_error(
                "product_unavailable",
                message.partition(":")[2].strip(),
                409,
            )

        # Provider exception text is not safe to return; some integrations
        # echo request headers or identifiers. Keep the public response stable.
        raise _api_error("order_failed", "Order could not be completed. Check the order status before retrying.", 400)

    # --------------------------------------------------------
    # Get order ID
    # --------------------------------------------------------

    try:

        order_id = int(
            result["order_id"]
        )

    except (
        KeyError,
        TypeError,
        ValueError,
    ):

        logger.error(
            "Purchase returned invalid result: %r",
            result,
        )

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            "Purchase returned an invalid order ID.",
        )

        raise _api_error(
            "order_failed",
            "Purchase returned an invalid order ID.",
            500,
        )

    # --------------------------------------------------------
    # Save API order mapping
    # --------------------------------------------------------

    await asyncio.to_thread(
        _set_api_order_result,
        request_row.id,
        order_id,
    )

    # --------------------------------------------------------
    # Load order
    # --------------------------------------------------------

    order = await asyncio.to_thread(
        _find_api_order,
        principal.key_id,
        order_id,
    )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return {
        "success": True,

        "idempotent_replay": False,

        "order": (
            _order_payload(order)
            if order
            else {
                "order_id": str(
                    order_id
                )
            }
        ),
    }


# ============================================================
# LIST ORDERS
# ============================================================

@router.get("/orders")
async def get_orders(
    page: int = Query(
        default=1,
        ge=1,
    ),

    limit: int = Query(
        default=50,
        ge=1,
        le=200,
    ),

    principal: ApiPrincipal = Depends(
        api_principal
    ),
):

    total, orders = await asyncio.to_thread(
        _list_api_orders,
        principal.key_id,
        (page - 1) * limit,
        limit,
    )

    return {

        "success": True,

        "page": page,

        "limit": limit,

        "total_orders": total,

        "total_pages": (
            (total + limit - 1)
            // limit
        ),

        "orders": [
            _order_payload(order)
            for order in orders
        ],
    }


# ============================================================
# GET SINGLE ORDER
# ============================================================

@router.get("/order/{order_id}")
async def get_order(
    order_id: int,

    principal: ApiPrincipal = Depends(
        api_principal
    ),
):

    order = await asyncio.to_thread(
        _find_api_order,
        principal.key_id,
        order_id,
    )

    if not order:

        raise _api_error(
            "order_not_found",
            (
                "No API order with that ID "
                "belongs to this API key."
            ),
            404,
        )

    return {
        "success": True,
        "order": _order_payload(order),
    }
