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
import logging
import re
import time

from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
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

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from config import (
    API_INVALID_AUTH_LIMIT_PER_MINUTE,
    API_MAX_CONCURRENT_ORDERS,
    API_ORDER_LIMIT_PER_MINUTE,
    API_RATE_LIMIT_PER_MINUTE,
    API_RATE_LIMIT_PER_SECOND,
)
from database import SessionLocal

from models.api_key import ApiKey, ApiOrder
from models.order import Order
from models.product import Product
from models.user import User

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


# ============================================================
# API PRINCIPAL
# ============================================================

@dataclass(frozen=True)
class ApiPrincipal:
    key_id: int
    telegram_id: int


# ============================================================
# REQUEST MODEL
# ============================================================

class OrderRequest(BaseModel):
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


async def _limit_order(key_id: int) -> None:
    """Allow only a small number of purchases and one active purchase per key."""

    now = time.monotonic()

    async with _rate_lock:

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


async def _release_order(key_id: int) -> None:
    """Release a purchase slot after the endpoint has completed."""

    async with _rate_lock:

        if _in_flight_orders[key_id] <= 1:
            _in_flight_orders.pop(key_id, None)
        else:
            _in_flight_orders[key_id] -= 1


def _client_address(request: Request) -> str:
    """Use the direct peer address; do not trust spoofable forwarding headers."""

    return request.client.host if request.client else "unknown"


async def _reject_invalid_auth_flood(client_address: str) -> None:
    """Stop repeated bad credentials before another database lookup is made."""

    now = time.monotonic()

    async with _rate_lock:

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
        _invalid_auth_windows[client_address].append(time.monotonic())


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

        "preorder": bool(
            product.preorder
        ),

        "delivery_type": (
            product.delivery_type
            or "automatic"
        ),
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

            stock = _real_stock(
                product
            )

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
) -> tuple[ApiOrder | None, bool]:

    db = SessionLocal()

    try:

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

        row = ApiOrder(
            api_key_id=api_key_id,
            client_order_id=client_order_id,
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

        row.status = (
            "completed"
            if order_id
            else "failed"
        )

        row.error_message = (
            (error_message or "")[:500]
            or None
        )

        db.commit()

    finally:

        db.close()


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

        return (
            product.source
            or "own"
        )

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

    # --------------------------------------------------------
    # Reserve idempotency key
    # --------------------------------------------------------

    request_row, is_new = await asyncio.to_thread(
        _reserve_order,
        principal.key_id,
        payload.client_order_id,
    )

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
            (
                request_row.error_message
                or
                "This client_order_id previously failed. "
                "Use a new ID after fixing the issue."
            ),
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
            )

        else:

            result = await asyncio.to_thread(
                _do_purchase,
                principal.telegram_id,
                payload.service_id,
                payload.quantity,
                payload.delivery_telegram_id,
            )

    except Exception as exc:

        logger.exception(
            "Order processing exception"
        )

        message = str(exc)

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            message,
        )

        raise _api_error(
            "order_failed",
            "Order processing failed.",
            500,
        )

    # --------------------------------------------------------
    # Purchase returned error
    # --------------------------------------------------------

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

        await asyncio.to_thread(
            _set_api_order_result,
            request_row.id,
            None,
            message,
        )

        if message == "insufficient_balance":

            raise _api_error(
                "insufficient_balance",
                "Insufficient wallet balance.",
                400,
            )

        raise _api_error(
            "order_failed",
            message,
            400,
        )

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
