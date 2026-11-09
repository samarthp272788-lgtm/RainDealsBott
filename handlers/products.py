import asyncio
import json
import logging
import threading
import time
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from html import escape as _esc
from datetime import datetime
from typing import Any

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from sqlalchemy import JSON, String, cast
from sqlalchemy.exc import SQLAlchemyError

import config
from config import ADMIN_IDS
from utils.ui import schedule_delete_card, show, update_card, answer_card
from database import SessionLocal, transaction, retry_on_write_conflict
from models.product import Product
from models.user import User
from models.order import Order
from models.product_engagement import ProductEngagement
from models.custom_rate import CustomRate
from models.rate_control import ProductRateControl, RateControlAssignment
from services.fulfillment_policy import order_capacity, manual_allowed, plan_delivery
from services.checkout_actions import confirmation_token, action_key, claim_checkout, finish_checkout, recover_checkout
from services.order_workflow import record_event
from services.warranty import start_coverage, policy_snapshot, policy_label
from services.input_forms import quantity as parse_quantity, is_cancel, InputError
from services.notification_outbox import enqueue, enqueue_once

# Reseller Manager & Reseller Config imports
try:
    from services.reseller_manager import ResellerManager, ResellerAPIError
except ImportError:
    ResellerManager = None
    ResellerAPIError = Exception

try:
    from services.reseller_config import get_reseller, get_all_resellers
except ImportError:
    get_reseller = None
    get_all_resellers = None

logger = logging.getLogger(__name__)

router = Router()

# ╔══════════════════════════════════════════════════════════════╗
# ║                     CONFIGURATION                            ║
# ╚══════════════════════════════════════════════════════════════╝

PREORDER_MAX_QTY = 10
DEFAULT_LOW_STOCK_THRESHOLD = 3
MAX_SEARCH_RESULTS = 15

try:
    REFERRAL_COMMISSION_RATE = Decimal(
        str(getattr(config, "REFERRAL_COMMISSION_RATE", "0.05"))
    )
except (InvalidOperation, ValueError):
    REFERRAL_COMMISSION_RATE = Decimal("0.05")

REFERRAL_CREDIT_TO_BALANCE = getattr(config, "REFERRAL_CREDIT_TO_BALANCE", True)

MONEY_QUANT = Decimal("0.00000001")

# ---- Stock notification channel settings (config.py) -----------------
STOCK_GROUP_ID = getattr(config, "STOCK_GROUP_ID", None)
STOCK_NOTIFICATIONS = getattr(config, "STOCK_NOTIFICATIONS", True)
GROUP_ID = getattr(config, "GROUP_ID", None)
GROUP_NOTIFICATIONS = getattr(config, "GROUP_NOTIFICATIONS", False)
ORDER_NOTIFICATION_CHANNEL_ID = getattr(config, "ORDER_NOTIFICATION_CHANNEL_ID", "")

# ╔══════════════════════════════════════════════════════════════╗
# ║            RESELLER LIVE STOCK CACHE & HELPERS               ║
# ╚══════════════════════════════════════════════════════════════╝

_reseller_stock_cache: dict[tuple[str, str], int] = {}
_reseller_stock_cache_updated: dict[tuple[str, str], float] = {}
_reseller_stock_unavailable: set[tuple[str, str]] = set()
_reseller_stock_cache_time: float = 0.0
_reseller_cache_lock = asyncio.Lock()
_reseller_refresh_task: asyncio.Task | None = None
_supplier_purchase_locks: dict[tuple[str, str], asyncio.Lock] = {}
RESELLER_CACHE_TTL = 30  # Includes empty/failed refreshes, preventing retry storms.
RESELLER_STOCK_MAX_AGE_SECONDS = 90
RESELLER_STOCK_TIMEOUT_SECONDS = 20  # Background work does not delay browsing.

# Per-user per-provider in-flight import locks
_reseller_import_locks: set[tuple[int, str]] = set()


def _reseller_cache_key(provider_id, service_id) -> tuple[str, str]:
    scope = str(provider_id or "default_provider").strip().casefold()
    return scope, str(service_id or "").strip()


def _cached_reseller_stock(provider_id, service_id):
    key = _reseller_cache_key(provider_id, service_id)
    if key in _reseller_stock_unavailable:
        return 0, False
    if key not in _reseller_stock_cache:
        return None, False
    fresh = time.monotonic() - _reseller_stock_cache_updated.get(key, 0) <= RESELLER_STOCK_MAX_AGE_SECONDS
    return (_reseller_stock_cache[key] if fresh else None), not fresh


def _supplier_stock_value(item: dict) -> int:
    """Parse stock conservatively; only explicit unlimited values are unbounded."""
    if "stock" not in item or item.get("stock") is None:
        return 0
    value = item.get("stock")
    if isinstance(value, str) and value.strip().casefold() in {
        "unlimited", "infinite", "infinity", "∞"
    }:
        return 999999
    try:
        stock = int(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    return max(0, stock)


def _supplier_delivery_units(delivered_items: list[str]) -> int:
    """Count delivered units from item arrays or newline-delimited payloads."""
    return sum(max(1, len([line for line in str(item).splitlines() if line.strip()]))
               for item in delivered_items if str(item).strip())


def _get_reseller_credentials(reseller_id: str | int | None = None) -> dict:
    """
    Dynamically loads provider configuration by ID or name,
    falling back to active provider configurations or config.py.
    """
    db = SessionLocal()
    try:
        from handlers.admin_products import _get_provider_by_id
        prov = _get_provider_by_id(db, str(reseller_id) if reseller_id is not None else None)
    except Exception:
        prov = None
    finally:
        db.close()

    if prov:
        if "em_store" in str(prov.get("id", "")).lower() or "ssondigitalworks" in str(prov.get("base_url", "")):
            prov["auth_type"] = "bearer"
        return prov

    base_url = None
    api_key = None
    api_secret = None
    name = "Excalibur Shop Bot"
    res = None

    if get_reseller and reseller_id:
        try:
            res = get_reseller(str(reseller_id))
        except Exception:
            res = None

    if res:
        base_url = getattr(res, "base_url", None) or (res.get("base_url") if isinstance(res, dict) else None)
        api_key = getattr(res, "api_key", None) or (res.get("api_key") if isinstance(res, dict) else None)
        api_secret = getattr(res, "api_secret", None) or (res.get("api_secret") if isinstance(res, dict) else None)
        name = getattr(res, "name", None) or (res.get("name") if isinstance(res, dict) else "Reseller")

    if not base_url or not api_key:
        base_url = getattr(config, "RESELLER_BASE_URL", None) or getattr(config, "RESELLER_URL",
                                                                         None) or "https://arrsnetworkzone.in"
        api_key = getattr(config, "RESELLER_API_KEY", None) or getattr(config, "RESELLER_KEY", None)
        api_secret = getattr(config, "RESELLER_API_SECRET", None)
        name = name or "Excalibur Shop Bot"

    clean_base_url = (base_url or "").replace("/docs", "").rstrip("/")
    is_em = "em_store" in str(reseller_id or "").lower() or "ssondigitalworks" in clean_base_url

    return {
        "id": str(reseller_id) if reseller_id else "excalibur",
        "base_url": clean_base_url,
        "api_key": api_key,
        "api_secret": api_secret,
        "name": name,
        "auth_type": "bearer" if is_em else "header",
    }


async def _call_reseller_get_products(manager) -> list | dict:
    """Helper to call get_products whether sync or async."""
    res = manager.get_products()
    if asyncio.iscoroutine(res):
        return await res
    return res


async def _call_reseller_place_order(manager, service_id: str, quantity: int, external_order_id: str) -> dict | list:
    """Helper to place an order via ResellerManager whether sync or async."""
    if hasattr(manager, "place_order"):
        res = manager.place_order(service_id=service_id, quantity=quantity, external_order_id=external_order_id)
    elif hasattr(manager, "create_order"):
        res = manager.create_order(service_id=service_id, quantity=quantity, external_order_id=external_order_id)
    elif hasattr(manager, "order"):
        res = manager.order(service_id=service_id, quantity=quantity, external_order_id=external_order_id)
    else:
        raise AttributeError("ResellerManager missing place_order method")

    if asyncio.iscoroutine(res):
        res = await res

    if not getattr(manager, "is_hmac_auth", False) or not isinstance(res, dict):
        return res

    order_id = res.get("order_id")
    if not order_id:
        return res

    for _ in range(20):
        fulfillment = res.get("fulfillment")
        if isinstance(fulfillment, (str, list, int, float)) and not isinstance(fulfillment, bool) and fulfillment:
            return res
        if isinstance(fulfillment, dict) and any(
            fulfillment.get(key) for key in (
                "payload", "delivery_data", "delivery", "delivery_items",
                "accounts", "credentials", "code", "result",
            )
        ):
            return res

        await asyncio.sleep(1)
        details = await manager.get_order(str(order_id))
        if not isinstance(details, dict):
            continue

        nested = details.get("order") or details.get("data")
        if isinstance(nested, dict):
            details = {**details, **nested}
        fulfillment = details.get("fulfillment")
        delivered = None
        if isinstance(fulfillment, (str, list, int, float)) and not isinstance(fulfillment, bool):
            delivered = fulfillment
        elif isinstance(fulfillment, dict):
            for key in ("payload", "delivery_data", "delivery", "delivery_items",
                        "accounts", "credentials", "code", "result"):
                if fulfillment.get(key) is not None:
                    delivered = fulfillment[key]
                    break

        merged = dict(res)
        merged.update(details)
        if delivered:
            merged["delivery"] = delivered
            merged["delivery_items"] = delivered if isinstance(delivered, list) else [delivered]
            return merged

        status = str(details.get("status") or "").lower()
        if status in {"canceled", "cancelled", "failed", "error"}:
            raise ResellerAPIError(
                message=f"Provider order {order_id} ended with status: {status}",
                provider_id=getattr(manager, "provider_id", None),
            )

    return res


def _load_stock_refresh_providers():
    """Run synchronous provider/config lookups outside the Telegram event loop."""
    db = SessionLocal()
    try:
        from handlers.admin_products import _get_all_active_providers
        providers = _get_all_active_providers(db)
    finally:
        db.close()
    if not providers:
        creds = _get_reseller_credentials()
        if creds.get("api_key") and creds.get("base_url"):
            providers = [creds]
    return providers


def _stock_refresh_error_detail(provider: dict, exc: Exception) -> str:
    detail = str(exc).strip() or type(exc).__name__
    for key in ('api_key', 'api_secret'):
        secret = str(provider.get(key) or '')
        if secret:
            detail = detail.replace(secret, '[redacted]')
    return detail.replace('\r', ' ').replace('\n', ' ')[:300]


async def _refresh_reseller_stock_cache_if_needed():
    """Bounded shared refresh; browsing schedules this without waiting for it."""
    global _reseller_stock_cache_time
    if not ResellerManager:
        return _reseller_stock_cache
    async with _reseller_cache_lock:
        loop = asyncio.get_running_loop()
        if _reseller_stock_cache_time and loop.time() - _reseller_stock_cache_time < RESELLER_CACHE_TTL:
            return _reseller_stock_cache
        try:
            providers = await asyncio.to_thread(_load_stock_refresh_providers)
            semaphore = asyncio.Semaphore(4)

            async def refresh_provider(prov):
                from services.provider_health import observe_sync
                if not prov.get("api_key") or not prov.get("base_url"):
                    return
                async with semaphore:
                    try:
                        manager = ResellerManager(
                            api_key=prov["api_key"], base_url=prov["base_url"],
                            provider_config=prov, timeout=RESELLER_STOCK_TIMEOUT_SECONDS,
                        )
                        reseller_data = await asyncio.wait_for(
                            _call_reseller_get_products(manager),
                            timeout=RESELLER_STOCK_TIMEOUT_SECONDS + 1,
                        )
                        services = None
                        if isinstance(reseller_data, list):
                            services = reseller_data
                        elif isinstance(reseller_data, dict):
                            for catalog_key in ("services", "products", "data"):
                                candidate = reseller_data.get(catalog_key)
                                if isinstance(candidate, list):
                                    services = candidate
                                    break
                        if services is None:
                            raise ValueError("Supplier catalog response did not contain a product list")
                        scope = str(prov.get("id") or prov.get("provider_id") or "default_provider").strip().casefold()
                        seen_service_keys = set()
                        for item in services:
                            if not isinstance(item, dict):
                                continue
                            sid = str(item.get("service_id") or item.get("provider_product_id") or item.get("id", "")).strip()
                            stock = _supplier_stock_value(item)
                            availability = item.get("is_available")
                            if availability is False or str(availability or "").strip().casefold() in {
                                "false", "no", "0", "inactive", "disabled", "unavailable"
                            }:
                                stock = 0
                            if sid:
                                key = _reseller_cache_key(scope, sid)
                                _reseller_stock_cache[key] = stock
                                _reseller_stock_cache_updated[key] = loop.time()
                                _reseller_stock_unavailable.discard(key)
                                seen_service_keys.add(key)
                        # A successful complete catalog response is authoritative:
                        # services absent from it are no longer sellable.
                        for key in [key for key in _reseller_stock_cache if key[0] == scope and key not in seen_service_keys]:
                            _reseller_stock_cache.pop(key, None)
                            _reseller_stock_cache_updated.pop(key, None)
                            _reseller_stock_unavailable.add(key)
                        observe_sync(prov.get("id") or prov.get("provider_id") or "default_provider")
                    except Exception as exc:
                        observe_sync(prov.get("id") or prov.get("provider_id") or "default_provider", exc)
                        logger.warning(
                            "Supplier stock refresh failed provider_id=%s name=%s error_type=%s "
                            "http_status=%s reason=%s; "
                            "keeping previous/database stock and retrying after %ss",
                            prov.get("id") or prov.get("provider_id"), prov.get("name", "Reseller"),
                            type(exc).__name__, getattr(exc, 'status_code', None),
                            _stock_refresh_error_detail(prov, exc), RESELLER_CACHE_TTL,
                        )

            await asyncio.gather(*(refresh_provider(prov) for prov in providers))
        except Exception:
            logger.exception("Could not refresh supplier stock; keeping previous/database stock")
        finally:
            # Timestamp completion, including empty data and failures. A timeout
            # must not immediately trigger a second request for waiting callbacks.
            _reseller_stock_cache_time = loop.time()
        return _reseller_stock_cache


def _schedule_reseller_stock_refresh():
    """Keep a single refresh task, with no queue of browsing callbacks."""
    global _reseller_refresh_task
    if not ResellerManager:
        return
    loop = asyncio.get_running_loop()
    if _reseller_stock_cache_time and loop.time() - _reseller_stock_cache_time < RESELLER_CACHE_TTL:
        return
    if _reseller_refresh_task is None or _reseller_refresh_task.done():
        _reseller_refresh_task = asyncio.create_task(_refresh_reseller_stock_cache_if_needed())


# ╔══════════════════════════════════════════════════════════════╗
# ║                        UI HELPERS                            ║
# ╚══════════════════════════════════════════════════════════════╝

def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _active_custom_price(db, telegram_id: int, product_id: int) -> Decimal | None:
    """Resolve a user's price override in at most two database round trips."""
    rate_rows = (
        db.query(CustomRate.product_id, CustomRate.price)
        .filter(
            CustomRate.telegram_id == telegram_id,
            CustomRate.is_active == True,
            (CustomRate.product_id == product_id) | CustomRate.product_id.is_(None),
        )
        .order_by(CustomRate.id.desc())
        .all()
    )
    specific_price = None
    default_price = None
    for rate_product_id, price in rate_rows:
        if rate_product_id == product_id and specific_price is None:
            specific_price = price
        elif rate_product_id is None and default_price is None:
            default_price = price
    if specific_price is not None:
        return _money(specific_price)
    if default_price is not None:
        return _money(default_price)

    assignment_exists = db.query(RateControlAssignment.id).filter(
        RateControlAssignment.telegram_id == telegram_id,
        RateControlAssignment.is_active == True,
    ).exists()
    controlled_price = (
        db.query(ProductRateControl.price)
        .filter(
            ProductRateControl.product_id == product_id,
            ProductRateControl.is_active == True,
            assignment_exists,
        )
        .first()
    )
    return _money(controlled_price[0]) if controlled_price else None

def _get_custom_price(telegram_id: int, product_id: int) -> Decimal | None:
    """Use the short-lived catalog cache for repeated product detail lookups."""
    return _get_catalog_custom_prices(telegram_id, [product_id]).get(int(product_id))

def _get_catalog_custom_prices(telegram_id: int, product_ids: list[int]) -> dict[int, Decimal]:
    """Resolve catalog pricing with at most two DB round trips on cache miss."""
    ids = list({int(product_id) for product_id in product_ids})
    if not ids:
        return {}
    cache_key = (int(telegram_id), tuple(sorted(ids)))
    now = time.monotonic()
    with _catalog_custom_prices_lock:
        cached = _catalog_custom_prices_cache.get(cache_key)
        if cached and now - cached[0] < CATALOG_CUSTOM_PRICES_CACHE_TTL_SECONDS:
            return cached[1].copy()

    db = SessionLocal()
    try:
        rate_rows = (
            db.query(CustomRate.product_id, CustomRate.price)
            .filter(
                CustomRate.telegram_id == telegram_id,
                CustomRate.is_active == True,
                (CustomRate.product_id.in_(ids)) | CustomRate.product_id.is_(None),
            )
            .order_by(CustomRate.id.desc())
            .all()
        )
        prices: dict[int, Decimal] = {}
        default_price = None
        for product_id, price in rate_rows:
            if product_id is None:
                if default_price is None:
                    default_price = _money(price)
            else:
                prices.setdefault(int(product_id), _money(price))

        if default_price is not None:
            prices = {product_id: prices.get(product_id, default_price) for product_id in ids}
        else:
            assignment_exists = db.query(RateControlAssignment.id).filter(
                RateControlAssignment.telegram_id == telegram_id,
                RateControlAssignment.is_active == True,
            ).exists()
            controlled_rates = (
                db.query(ProductRateControl.product_id, ProductRateControl.price)
                .filter(
                    ProductRateControl.product_id.in_(ids),
                    ProductRateControl.is_active == True,
                    assignment_exists,
                )
                .all()
            )
            for product_id, price in controlled_rates:
                prices.setdefault(int(product_id), _money(price))

        with _catalog_custom_prices_lock:
            if len(_catalog_custom_prices_cache) >= CATALOG_CUSTOM_PRICES_CACHE_MAX_ENTRIES:
                _catalog_custom_prices_cache.clear()
            _catalog_custom_prices_cache[cache_key] = (time.monotonic(), prices.copy())
        return prices
    finally:
        db.close()

CATALOG_CUSTOM_PRICES_CACHE_TTL_SECONDS = 15
CATALOG_CUSTOM_PRICES_CACHE_MAX_ENTRIES = 1024
_catalog_custom_prices_cache: dict[tuple[int, tuple[int, ...]], tuple[float, dict[int, Decimal]]] = {}
_catalog_custom_prices_lock = threading.Lock()


def invalidate_catalog_custom_prices_cache() -> None:
    with _catalog_custom_prices_lock:
        _catalog_custom_prices_cache.clear()


def _divider(char: str = "━", length: int = 30) -> str:
    return char * length


def _border_box(title: str, emoji: str = "📦") -> str:
    return (
        f"╔{_divider('═', 28)}╗\n"
        f"║  {emoji} {title:<23}║\n"
        f"╚{_divider('═', 28)}╝"
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║            CATEGORY -> BUTTON STYLE MAPPING                  ║
# ╚══════════════════════════════════════════════════════════════╝

CATEGORY_CONFIG = {
    "premium": {"icon": "👑", "label": "Premium", "style": "success", "color": "🟢"},
    "budget": {"icon": "💸", "label": "Budget", "style": "danger", "color": "🔴"},
    "vpn": {"icon": "🔒", "label": "VPN", "style": "primary", "color": "🔵"},
    "email": {"icon": "📧", "label": "Email", "style": "success", "color": "🟢"},
    "streaming": {"icon": "🎬", "label": "Streaming", "style": "primary", "color": "🔵"},
    "gaming": {"icon": "🎮", "label": "Gaming", "style": "success", "color": "🟢"},
    "software": {"icon": "💻", "label": "Software", "style": "primary", "color": "🔵"},
    "education": {"icon": "📚", "label": "Education", "style": "success", "color": "🟢"},
    "out of stock": {"icon": "🚫", "label": "Out of Stock", "style": "danger", "color": "⚫"},
    "reseller": {"icon": "🔗", "label": "Reseller API", "style": "primary", "color": "🔵"},
}

DEFAULT_CATEGORY_CONFIG = {"icon": "📦", "label": "General", "style": "primary", "color": "🔵"}


def _get_category_config(category: str | None) -> dict:
    if not category:
        return DEFAULT_CATEGORY_CONFIG
    category_name = category.strip()
    config = CATEGORY_CONFIG.get(category_name.lower())
    if config:
        return config
    return {**DEFAULT_CATEGORY_CONFIG, "label": category_name}


def _category_style(category: str | None) -> str:
    return _get_category_config(category)["style"]


# ╔══════════════════════════════════════════════════════════════╗
# ║              STOCK DISPLAY HELPERS                           ║
# ╚══════════════════════════════════════════════════════════════╝

def _stock_indicator(stock: int) -> str:
    if stock <= 0:
        return "🔴 <b>Out of Stock</b>"
    elif stock >= 999999:
        return "🟢 <b>In Stock</b>"
    elif stock <= 3:
        return f"🟡 <b>Low Stock</b> ({stock} left)"
    elif stock <= 10:
        return f"🟢 <b>In Stock</b> ({stock} available)"
    else:
        return f"🟢 <b>Well Stocked</b> ({stock} available)"


def _provider_stock_value(stock: Any) -> int:
    if stock is None:
        return 0
    if isinstance(stock, bool):
        return 1 if stock else 0
    try:
        value = int(stock)
    except (TypeError, ValueError):
        normalized = str(stock).strip().lower()
        if normalized in {"out_of_stock", "oos", "unavailable", "disabled", "inactive", "false", "no"}:
            return 0
        if normalized in {"unlimited", "infinite", "infinity", "in_stock", "available", "active", "true", "yes"}:
            return 999999 if normalized in {"unlimited", "infinite", "infinity"} else 1
        return 0
    return max(0, value)


# ╔══════════════════════════════════════════════════════════════╗
# ║              BULK PRICING HELPERS                            ║
# ╚══════════════════════════════════════════════════════════════╝

def _get_bulk_price(bulk_pricing, quantity: int):
    """Get the applicable bulk price for a given quantity."""
    if not bulk_pricing:
        return None

    try:
        tiers = json.loads(bulk_pricing) if isinstance(bulk_pricing, str) else bulk_pricing
    except (json.JSONDecodeError, TypeError):
        return None

    if not tiers:
        return None

    best_price = None
    best_min = 0

    for tier in tiers.values():
        min_qty = int(tier.get("min", 1))
        max_qty = tier.get("max")
        if max_qty is not None:
            max_qty = int(max_qty)
        price = float(tier.get("price", 0))

        if quantity >= min_qty:
            if max_qty is None or quantity <= max_qty:
                if min_qty > best_min:
                    best_price = price
                    best_min = min_qty

    return best_price


def _format_bulk_pricing_text(bulk_pricing) -> str:
    """Format bulk pricing tiers for display to users."""
    if not bulk_pricing:
        return "  ❌ <i>Not Available</i>"

    try:
        tiers = json.loads(bulk_pricing) if isinstance(bulk_pricing, str) else bulk_pricing
    except (json.JSONDecodeError, TypeError):
        return "  ❌ <i>Not Available</i>"

    if not tiers:
        return "  ❌ <i>Not Available</i>"

    lines = []
    sorted_tiers = sorted(tiers.values(), key=lambda x: int(x.get("min", 1)))

    for tier in sorted_tiers:
        min_q = tier.get("min", 1)
        max_q = tier.get("max")
        price = tier.get("price", 0)

        if max_q is not None:
            lines.append(f"  🏷 {min_q}-{max_q} units → <b>${float(price):.2f}</b>/each")
        else:
            lines.append(f"  🏷 {min_q}+ units → <b>${float(price):.2f}</b>/each")

    return "\n".join(lines)


# ╔══════════════════════════════════════════════════════════════╗
# ║                        FSM STATES                            ║
# ╚══════════════════════════════════════════════════════════════╝

class PurchaseStates(StatesGroup):
    waiting_qty = State()


class SearchStates(StatesGroup):
    waiting_query = State()


# ╔══════════════════════════════════════════════════════════════╗
# ║       DURABLE FAVORITES AND RESTOCK SUBSCRIPTIONS            ║
# ╚══════════════════════════════════════════════════════════════╝

def _engagement_row(db, user_id: int, product_id: int, *, create: bool = False):
    # Locking the existing user serializes first-time favorites/subscriptions
    # for that user and avoids duplicate composite-key inserts on rapid taps.
    user = db.query(User).filter(User.telegram_id == user_id).with_for_update().first()
    if not user:
        return None
    row = (
        db.query(ProductEngagement)
        .filter_by(telegram_id=user_id, product_id=product_id)
        .with_for_update()
        .first()
    )
    if row is None and create:
        row = ProductEngagement(telegram_id=user_id, product_id=product_id)
        db.add(row)
        db.flush()
    return row


def _toggle_favorite(user_id: int, product_id: int) -> bool:
    db = SessionLocal()
    try:
        row = _engagement_row(db, user_id, product_id, create=True)
        if row is None:
            return False
        row.is_favorite = not bool(row.is_favorite)
        added = bool(row.is_favorite)
        if not row.is_favorite and not row.restock_enabled:
            db.delete(row)
        db.commit()
        return added
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _is_favorite(user_id: int, product_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.query(ProductEngagement.id).filter_by(
            telegram_id=user_id, product_id=product_id, is_favorite=True
        ).first() is not None
    finally:
        db.close()


def _get_favorites(user_id: int) -> list[int]:
    db = SessionLocal()
    try:
        return [row[0] for row in db.query(ProductEngagement.product_id).filter_by(
            telegram_id=user_id, is_favorite=True
        ).order_by(ProductEngagement.created_at.desc()).all()]
    finally:
        db.close()


def _set_restock_subscription(user_id: int, product_id: int, enabled: bool) -> bool:
    db = SessionLocal()
    try:
        row = _engagement_row(db, user_id, product_id, create=enabled)
        if row is None:
            return False
        was_enabled = bool(row.restock_enabled)
        row.restock_enabled = enabled
        if not row.is_favorite and not row.restock_enabled:
            db.delete(row)
        db.commit()
        return (not was_enabled) if enabled else was_enabled
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _restock_subscribers(product_id: int) -> list[int]:
    db = SessionLocal()
    try:
        return [row[0] for row in db.query(ProductEngagement.telegram_id).filter_by(
            product_id=product_id, restock_enabled=True
        ).all()]
    finally:
        db.close()


def _queue_restock_subscription(user_id, product_id, bot_id, text, keyboard):
    from uuid import uuid4
    from database import transaction
    from services.notification_outbox import enqueue
    with transaction() as db:
        row = db.query(ProductEngagement).filter_by(telegram_id=user_id, product_id=product_id).with_for_update().first()
        if not row or not row.restock_enabled:
            return False
        from services.customer_tools import allows
        if not allows(db,user_id,'restock'):
            return False
        enqueue(db, f'restock:{product_id}:{uuid4().hex}', user_id, text, bot_id, reply_markup=keyboard)
        row.restock_enabled = False
        if not row.is_favorite:
            db.delete(row)
    return True


async def _notify_restock_subscribers(bot, product, stock: int) -> None:
    subscribers = await asyncio.to_thread(_restock_subscribers, product.id)
    if not subscribers:
        return
    text = (
        f"🔔 <b>BACK IN STOCK</b>\n\n"
        f"{_esc(product.icon or '📦')} <b>{_esc(product.name)}</b>\n"
        f"💰 Price: <b>${_money(product.price):.2f}</b>\n"
        f"📦 Available now: <b>{stock}</b>\n\n"
        "You asked to be notified. Open the product to see the latest stock and order."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 View product", callback_data=f"product_{product.id}")],
        [InlineKeyboardButton(text="🔕 Keep browsing", callback_data="products_menu")],
    ])
    for user_id in subscribers:
        try:
            await asyncio.to_thread(_queue_restock_subscription, user_id, product.id, bot.id, text, keyboard)
        except Exception:
            # The subscription and queued notification roll back together.
            logger.warning("Could not queue restock alert for user %s product %s", user_id, product.id)


async def _search_products(query: str) -> list:
    db = SessionLocal()
    try:
        search_term = f"%{query}%"
        return (
            db.query(Product)
            .filter(
                Product.is_active == True,
                (
                        Product.name.ilike(search_term) |
                        Product.category.ilike(search_term) |
                        Product.description.ilike(search_term)
                )
            )
            .order_by(Product.id.asc())
            .limit(MAX_SEARCH_RESULTS)
            .all()
        )
    finally:
        db.close()


# ╔══════════════════════════════════════════════════════════════╗
# ║              PER-USER PURCHASE LOCK                          ║
# ╚══════════════════════════════════════════════════════════════╝

_purchase_locks: dict[int, asyncio.Lock] = {}
_purchase_locks_guard = asyncio.Lock()


async def _get_purchase_lock(telegram_id: int) -> asyncio.Lock:
    async with _purchase_locks_guard:
        lock = _purchase_locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            _purchase_locks[telegram_id] = lock
        return lock


# ╔══════════════════════════════════════════════════════════════╗
# ║              CACHED PRODUCTS FETCH (30s TTL)                 ║
# ╚══════════════════════════════════════════════════════════════╝

_products_cache: dict = {"data": None, "timestamp": 0}
_products_cache_lock = asyncio.Lock()
_products_cache_state_lock = threading.Lock()
_products_cache_generation = 0
_products_refresh_task = None
PRODUCTS_CACHE_TTL_SECONDS = 30


def invalidate_products_cache() -> None:
    global _products_cache_generation
    with _products_cache_state_lock:
        _products_cache_generation += 1
        _products_cache["data"] = None
        _products_cache["timestamp"] = 0


def _load_product_models_safely(query) -> list[Product]:
    """Materialize product rows without letting one malformed JSON value abort the query."""
    product_columns = list(Product.__table__.columns)
    json_fields = {
        column.name
        for column in product_columns
        if isinstance(column.type, JSON)
    }
    selected = [
        cast(getattr(Product, column.name), String).label(column.name)
        if column.name in json_fields
        else getattr(Product, column.name)
        for column in product_columns
    ]
    rows = query.with_entities(*selected).all()
    expected_json_shapes = {
        "required_info": list,
        "bulk_pricing": dict,
    }
    products = []
    for row in rows:
        values = {
            column.name: row[index]
            for index, column in enumerate(product_columns)
        }
        invalid_fields = []
        for field in json_fields:
            raw_value = values[field]
            if raw_value is None:
                continue
            try:
                parsed = (
                    json.loads(raw_value)
                    if isinstance(raw_value, (str, bytes, bytearray))
                    else raw_value
                )
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                invalid_fields.append(field)
                continue
            expected_shape = expected_json_shapes.get(field)
            if expected_shape and not isinstance(parsed, expected_shape):
                invalid_fields.append(field)
                continue
            values[field] = parsed
        if invalid_fields:
            logger.error(
                "Skipping product with invalid JSON configuration product_id=%s fields=%s",
                values.get("id"),
                ",".join(sorted(invalid_fields)),
            )
            continue
        products.append(Product(**values))
    return products


async def _refresh_active_products():
    """Refresh the catalog snapshot without blocking readers of stale data."""
    async with _products_cache_lock:
        while True:
            now = asyncio.get_running_loop().time()
            with _products_cache_state_lock:
                if (
                    _products_cache["data"] is not None
                    and now - _products_cache["timestamp"] < PRODUCTS_CACHE_TTL_SECONDS
                ):
                    return _products_cache["data"]
                generation = _products_cache_generation

            def _query():
                db = SessionLocal()
                try:
                    # Read JSON columns as text first so an invalid legacy
                    # product is isolated instead of crashing the catalog.
                    return _load_product_models_safely(
                        db.query(Product)
                        .filter(Product.is_active == True, Product.price > 0)
                        .order_by(Product.id.asc())
                    )
                finally:
                    db.close()

            products = await asyncio.to_thread(_query)
            with _products_cache_state_lock:
                if generation != _products_cache_generation:
                    continue
                _products_cache["data"] = products
                _products_cache["timestamp"] = asyncio.get_running_loop().time()
                return products


async def _refresh_products_cache_background():
    try:
        await _refresh_active_products()
    except Exception:
        logger.exception("Background product catalog refresh failed")


async def _fetch_active_products():
    """Return cached products immediately and refresh expired data in background."""
    global _products_refresh_task
    now = asyncio.get_running_loop().time()
    with _products_cache_state_lock:
        cached_products = _products_cache["data"]
        is_fresh = (
            cached_products is not None
            and now - _products_cache["timestamp"] < PRODUCTS_CACHE_TTL_SECONDS
        )
    if cached_products is not None:
        if not is_fresh and (_products_refresh_task is None or _products_refresh_task.done()):
            _products_refresh_task = asyncio.create_task(_refresh_products_cache_background())
        return cached_products
    return await _refresh_active_products()


def _fetch_product(product_id: int):
    db = SessionLocal()
    try:
        products = _load_product_models_safely(
            db.query(Product).filter(Product.id == product_id).limit(1)
        )
        return products[0] if products else None
    finally:
        db.close()


def _fetch_products_by_ids(product_ids: list[int]) -> list:
    db = SessionLocal()
    try:
        return _load_product_models_safely(
            db.query(Product)
            .filter(Product.id.in_(product_ids), Product.is_active == True)
        )
    finally:
        db.close()


def _fetch_free_products() -> list:
    """Fetch all active products with price = 0 (freebies)."""
    db = SessionLocal()
    try:
        return _load_product_models_safely(
            db.query(Product)
            .filter(Product.is_active == True, Product.price == 0)
            .order_by(Product.id.asc())
        )
    finally:
        db.close()


def _accounts(product) -> list[str]:
    if not product.file_content:
        return []
    return [a.strip() for a in product.file_content.splitlines() if a.strip()]


def _real_stock(product) -> int:
    """
    Fast stock check:
    - RESELLER PRODUCT: Looks up live/cached stock from Reseller API via reseller_service_id.
      Falls back to database Product.stock if not found in live cache.
    - OWN PRODUCT (Manual): Returns product.stock.
    - OWN PRODUCT (Automatic/Hybrid): Counts local accounts in file_content.
    """
    source = getattr(product, "source", "own") or "own"
    if source == "reseller":
        service_id = str(getattr(product, "reseller_service_id", "") or "").strip()
        db_stock = getattr(product, "stock", 0)
        fallback_stock = db_stock if (db_stock is not None and db_stock > 0) else 0

        if not service_id:
            return fallback_stock

        provider_scope = getattr(product, "provider_id", None) or getattr(product, "reseller_name", None)
        live_stock, stale = _cached_reseller_stock(provider_scope, service_id)
        if stale:
            return 0
        if live_stock is not None:
            # For supplier products the supplier's fresh count is authoritative;
            # the local Product.stock column is only an import-time snapshot.
            return live_stock

        return fallback_stock

    return order_capacity(product)


def _get_max_qty(product) -> int:
    cap = _real_stock(product)
    if float(product.price) == 0:
        return 1 if cap > 0 or (product.preorder and manual_allowed(product)) else 0
    source = getattr(product, "source", "own") or "own"
    if cap <= 0 and product.preorder and source != "reseller" and manual_allowed(product):
        return PREORDER_MAX_QTY
    return max(cap, 0)


# ╔══════════════════════════════════════════════════════════════╗
# ║      STOCK CHANGE TRACKING — RESTOCK / NEW PRODUCT /         ║
# ║      LIMITED STOCK BROADCASTS (→ STOCK_GROUP_ID, user-facing)║
# ╚══════════════════════════════════════════════════════════════╝

_stock_state: dict[int, dict] = {}
_stock_state_lock = asyncio.Lock()
_stock_scan_seeded = False
_stock_bot_username: str | None = None
_stock_bot_username_lock = asyncio.Lock()


def _stock_event_text(kind: str, product, **kwargs) -> str:
    stock = kwargs.get("stock", 0)
    category = _get_category_config(product.category)["label"]
    price = _money(product.price)

    if kind == "new_product":
        return (
            f"🆕 <b>NEW PRODUCT</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{_esc(product.name)}</b>\n"
            f"🆔 Product ID: <code>#{product.id}</code>\n"
            f"🏷 Category: {_esc(category)}\n"
            f"💎 Price: <b>${price:.2f}</b>\n"
            f"📦 Available: <b>{stock}</b>\n\n"
            "Tap below to view and order."
        )

    if kind == "restock":
        added = kwargs["added"]
        return (
            f"🔥 <b>BACK IN STOCK</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{_esc(product.name)}</b>\n\n"
            f"🆔 Product ID: <code>#{product.id}</code>\n"
            f"🟢 Added: <b>+{added}</b> units\n"
            f"💎 Price: <b>${price:.2f}</b>\n"
            f"📦 Total available: <b>{stock}</b>\n\n"
            "Order now while it’s available."
        )

    if kind == "limited_stock":
        return (
            f"⚠️ <b>LOW STOCK</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{_esc(product.name)}</b>\n"
            f"🆔 Product ID: <code>#{product.id}</code>\n"
            f"💎 Price: <b>${price:.2f}</b>\n"
            f"📦 Only <b>{stock}</b> left."
        )

    return ""


async def _send_stock_channel_message(bot, text: str, product_id: int | None = None, can_buy: bool = True):
    """Queue stock posts so Telegram network latency never blocks bot actions."""
    if not text:
        return

    destinations = []
    if STOCK_NOTIFICATIONS and STOCK_GROUP_ID:
        destinations.append(STOCK_GROUP_ID)
    if GROUP_NOTIFICATIONS and GROUP_ID and str(GROUP_ID) not in {str(target) for target in destinations}:
        destinations.append(GROUP_ID)
    if not destinations:
        return

    task = asyncio.create_task(
        _deliver_stock_channel_message(bot, text, destinations, product_id, can_buy)
    )
    task.add_done_callback(_log_stock_notification_task)


async def _deliver_stock_channel_message(bot, text: str, destinations: list, product_id: int | None, can_buy: bool):
    global _stock_bot_username
    if not destinations:
        return
    markup = None
    if product_id is not None and can_buy:
        try:
            if not _stock_bot_username:
                async with _stock_bot_username_lock:
                    if not _stock_bot_username:
                        me = await bot.get_me()
                        _stock_bot_username = me.username
            if _stock_bot_username:
                markup = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🛍 Buy now",
                        url=f"https://t.me/{_stock_bot_username}?start=product_{product_id}",
                    )
                ]])
        except Exception:
            logger.exception("Could not create product deep link for stock notification")

    from uuid import uuid4
    event = f'stock:{product_id}:{uuid4().hex}'
    # Destination rejection/backoff and retries are handled by the durable outbox.
    for target in destinations:
        await asyncio.to_thread(enqueue_once, event, target, text, bot.id, markup)


def _log_stock_notification_task(task: asyncio.Task):
    try:
        task.result()
    except Exception:
        logger.exception("Unexpected error while delivering stock notifications")


async def _refresh_and_scan_product(bot, product_id: int):
    try:
        product = await asyncio.to_thread(_fetch_product, product_id)
        if product:
            _fire_stock_scan(bot, [product])
    except Exception:
        logger.exception("Could not refresh product stock after purchase")


async def _scan_stock_changes(bot, products: list):
    global _stock_scan_seeded

    if not products:
        return

    events = []

    async with _stock_state_lock:
        first_run = not _stock_scan_seeded

        for p in products:
            current = _real_stock(p)
            threshold = p.low_stock_threshold if p.low_stock_threshold is not None else DEFAULT_LOW_STOCK_THRESHOLD
            state = _stock_state.get(p.id)

            if state is None:
                _stock_state[p.id] = {
                    "last": current,
                    "low_stock_notified": current <= threshold,
                }
                if not first_run:
                    events.append(("new_product", p, {"stock": current}))
                continue

            last = state["last"]

            if current > last:
                added = current - last
                state["last"] = current
                state["low_stock_notified"] = current <= threshold
                events.append(("restock", p, {"added": added, "stock": current}))

            elif current < last:
                state["last"] = current
                if current <= threshold and not state["low_stock_notified"]:
                    state["low_stock_notified"] = True
                    events.append(("limited_stock", p, {"stock": current}))

        _stock_scan_seeded = True

    for kind, product, kwargs in events:
        await _send_stock_channel_message(
            bot,
            _stock_event_text(kind, product, **kwargs),
            product_id=product.id,
            can_buy=kwargs.get("stock", 0) > 0,
        )
        if kind == "restock":
            await _notify_restock_subscribers(bot, product, int(kwargs.get("stock", 0)))


def _fire_stock_scan(bot, products: list):
    """Fire-and-forget wrapper so UI handlers never wait on notification sends."""
    try:
        asyncio.create_task(_scan_stock_changes(bot, products))
    except RuntimeError:
        logger.exception("Could not schedule stock scan task")


async def notify_new_product(bot, product):
    stock = _real_stock(product)
    threshold = product.low_stock_threshold if product.low_stock_threshold is not None else DEFAULT_LOW_STOCK_THRESHOLD

    async with _stock_state_lock:
        _stock_state[product.id] = {
            "last": stock,
            "low_stock_notified": stock <= threshold,
        }

    await _send_stock_channel_message(
        bot,
        _stock_event_text("new_product", product, stock=stock),
        product_id=product.id,
        can_buy=stock > 0,
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              FREEBIES MENU                                   ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "freebies_menu")
async def freebies_menu(callback: CallbackQuery):
    await callback.answer()

    _schedule_reseller_stock_refresh()
    products = await asyncio.to_thread(_fetch_free_products)

    if not products:
        await show(
            callback,
            (
                f"🎁 <b>FREEBIES</b>\n\n"
                f"📭 <b>No free products available right now.</b>\n\n"
                f"{_divider('─', 28)}\n\n"
                f"💡 Check back later — free products\n"
                f"are added regularly!\n\n"
                f"<i>Stay tuned for giveaways 🎉</i>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛍 Browse Paid Products", callback_data="products_menu",
                                          style="primary")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
                ]
            ),
        )
        return

    _fire_stock_scan(callback.bot, products)

    text = (
        f"🎁 <b>FREEBIES</b>\n\n"
        f"<b>🎉 Free Products Available!</b>\n\n"
        f"{_divider('─', 28)}\n"
        f"<b>📊 Total Free Items:</b> {len(products)}\n\n"
        f"{_divider('─', 28)}\n\n"
        f"<b>👇 Grab your free product below:</b>\n"
        f"<i>Tap any item to claim it — no payment needed!</i>"
    )

    keyboard = []
    for p in products:
        cat_config = _get_category_config(p.category)
        stock = _real_stock(p)
        stock_badge = "🔴 OOS" if stock <= 0 else (
            f"🟢 In Stock" if stock >= 999999 else (f"🟡 {stock}" if stock <= 3 else f"🟢 {stock}"))

        keyboard.append([
            InlineKeyboardButton(
                text=f"{p.icon or cat_config['icon']} {p.name} — FREE! 🎁 | {stock_badge}",
                callback_data=f"product_{p.id}",
                style=cat_config["style"],
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="🛍 Browse Paid Products", callback_data="products_menu", style="primary"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary"),
    ])

    await show(callback, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# ╔══════════════════════════════════════════════════════════════╗
# ║              PRODUCTS MENU (PAID ONLY)                       ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "products_menu")
async def products_menu(callback: CallbackQuery):
    started = time.perf_counter()
    catalog_started = started
    catalog_ms = 0.0
    try:
        await callback.answer()
        catalog_started = time.perf_counter()
        products = await _fetch_active_products()
        catalog_ms = (time.perf_counter() - catalog_started) * 1000
        await _show_product_categories(callback, products)
    finally:
        total_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "button_timing button=products user_id=%s catalog_ms=%.1f total_ms=%.1f",
            callback.from_user.id,
            catalog_ms if catalog_ms else (time.perf_counter() - catalog_started) * 1000,
            total_ms,
        )


def _category_key(product) -> str | None:
    value = getattr(product, "category", None)
    if value is None or not str(value).strip():
        return None
    return str(value).strip().casefold()


async def _show_product_categories(callback: CallbackQuery, products: list):
    if not products:
        await show(
            callback,
            "📦 <b>PRODUCT CATEGORIES</b>\n\n📭 <b>No paid products available right now.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Go Back", callback_data="main_menu", style="primary")
            ]]),
        )
        return

    groups: dict[str, list] = {}
    standalone = []
    for product in products:
        key = _category_key(product)
        if key is None:
            standalone.append(product)
        else:
            groups.setdefault(key, []).append(product)

    rows = []
    pending_row = []
    sorted_groups = sorted(
        groups.items(),
        key=lambda item: _get_category_config(item[1][0].category)["label"].casefold(),
    )
    for _, category_products in sorted_groups:
        first_product = category_products[0]
        config = _get_category_config(first_product.category)
        category_name = (getattr(first_product, "category", None) or "").strip().lower()
        is_custom_category = category_name not in CATEGORY_CONFIG
        icon = first_product.icon if is_custom_category and first_product.icon else config["icon"]
        label = config["label"]
        if len(label) > 40:
            label = label[:37].rstrip() + "…"
        pending_row.append(InlineKeyboardButton(
            text=f"{icon} {label} ({len(category_products)})",
            callback_data=f"products_cat_{first_product.id}",
            style=config["style"],
        ))
        if len(pending_row) == 3:
            rows.append(pending_row)
            pending_row = []
    if pending_row:
        rows.append(pending_row)
    if standalone:
        custom_prices = await asyncio.to_thread(
            _get_catalog_custom_prices,
            callback.from_user.id,
            [product.id for product in standalone],
        )
        _fire_stock_scan(callback.bot, standalone)
        for product in standalone:
            price = custom_prices.get(product.id)
            price = price if price is not None else _money(product.price)
            stock = _real_stock(product)
            stock_badge = "🔴 OOS" if stock <= 0 else ("🟢 In Stock" if stock >= 999999 else f"🟢 {stock}")
            label = f"{product.icon or '📦'} {product.name} — ${price:.2f} | {stock_badge}"
            rows.append([InlineKeyboardButton(
                text=label[:64],
                callback_data=f"product_{product.id}",
                style="primary",
            )])
    rows.extend([
        [InlineKeyboardButton(text=f"🛍 All Products ({len(products)})", callback_data="products_all", style="primary")],
        [InlineKeyboardButton(text="⭐ Favorites", callback_data="favorites_menu", style="success"),
         InlineKeyboardButton(text="🔔 Restock alerts", callback_data="restock_alerts_menu", style="primary")],
        [InlineKeyboardButton(text="🔍 Search products", callback_data="search_start", style="primary")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
    ])
    await show(
        callback,
        "🛍 <b>SHOP BY CATEGORY</b>\n\nChoose a category to browse, or open the full catalog.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "products_all")
async def products_all(callback: CallbackQuery):
    await callback.answer()
    await _show_products_catalog(callback, await _fetch_active_products(), show_all=True)


@router.callback_query(F.data.startswith("products_cat_"))
async def products_category(callback: CallbackQuery):
    started = time.perf_counter()
    await callback.answer()
    try:
        representative_id = int(callback.data.removeprefix("products_cat_"))
    except (TypeError, ValueError):
        await _show_product_categories(callback, await _fetch_active_products())
        return
    products = await _fetch_active_products()
    representative = next((p for p in products if int(p.id) == representative_id), None)
    if representative is None:
        await _show_product_categories(callback, products)
        return
    await _show_products_catalog(callback, products, category=_category_key(representative))
    logger.info("button_timing button=category user_id=%s total_ms=%.1f",
                callback.from_user.id, (time.perf_counter() - started) * 1000)


async def _show_products_catalog(
    callback: CallbackQuery,
    products: list,
    category: str | None = None,
    show_all: bool = False,
):
    if not products:
        await show(
            callback,
            "📦 <b>PRODUCTS</b>\n\n📭 <b>No paid products available right now.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Categories", callback_data="products_menu", style="primary"),
                InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary"),
            ]]),
        )
        return

    visible_products = products if show_all else [p for p in products if _category_key(p) == category]
    category_label = (
        "All Products" if show_all else
        _get_category_config(visible_products[0].category)["label"] if visible_products else "Products"
    )
    if not visible_products:
        await show(
            callback,
            f"📦 <b>{_esc(category_label)}</b>\n\n📭 No products are available in this category right now.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅ Categories", callback_data="products_menu", style="primary"),
                InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary"),
            ]]),
        )
        return

    # Keep reseller refresh in the background so category screens remain fast.
    if any((getattr(product, "source", "own") or "own") == "reseller" for product in visible_products):
        _schedule_reseller_stock_refresh()

    _fire_stock_scan(callback.bot, visible_products)
    custom_prices = await asyncio.to_thread(
        _get_catalog_custom_prices, callback.from_user.id, [product.id for product in visible_products]
    )
    text = f"🛍 <b>{_esc(category_label)}</b>\nPlease select a product to proceed"
    keyboard = []
    for p in visible_products:
        cat_config = _get_category_config(p.category)
        stock = _real_stock(p)
        custom_price = custom_prices.get(p.id)
        price = custom_price if custom_price is not None else _money(p.price)
        stock_badge = "🔴 OOS" if stock <= 0 else (
            "🟢 In Stock" if stock >= 999999 else (f"🟡 {stock}" if stock <= 3 else f"🟢 {stock}"))
        custom_badge = " 🏷" if custom_price is not None else ""
        bulk_badge = " 📦" if p.bulk_pricing and custom_price is None else ""
        keyboard.append([InlineKeyboardButton(
            text=f"#{p.id} {p.icon or cat_config['icon']} {p.name} — ${price:.2f}{custom_badge}{bulk_badge} | {stock_badge}",
            callback_data=f"product_{p.id}",
            style=cat_config["style"],
        )])

    if show_all:
        keyboard.append([InlineKeyboardButton(text="⬅ Categories", callback_data="products_menu", style="primary")])
    else:
        keyboard.append([
            InlineKeyboardButton(text="⬅ Categories", callback_data="products_menu", style="primary"),
            InlineKeyboardButton(text="🛍 All Products", callback_data="products_all", style="primary"),
        ])
    keyboard.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")])
    await show(callback, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# ╔══════════════════════════════════════════════════════════════╗
# ║              FAVORITES SYSTEM                                ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("toggle_fav_"))
async def toggle_favorite(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    added = await asyncio.to_thread(_toggle_favorite, user_id, product_id)
    await callback.answer(
        "⭐ Added to favorites!" if added else "💔 Removed from favorites",
        show_alert=True
    )
    await product_info(callback, linked_product_id=product_id, acknowledge_callback=False)


@router.callback_query(F.data == "favorites_menu")
async def favorites_menu(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    fav_ids = await asyncio.to_thread(_get_favorites, user_id)

    if not fav_ids:
        await show(
            callback,
            (
                f"⭐ <b>FAVORITES</b>\n\n"
                f"📭 <b>No favorites yet!</b>\n\n"
                f"{_divider('─', 28)}\n\n"
                f"💡 <b>How to add:</b>\n"
                f"1. Browse products\n"
                f"2. Tap ⭐ on a product\n"
                f"3. It'll appear here!\n\n"
                f"<i>Your favorites are saved automatically.</i>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="success")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
                ]
            ),
        )
        return

    _schedule_reseller_stock_refresh()
    products = await asyncio.to_thread(_fetch_products_by_ids, fav_ids)

    if not products:
        await show(callback, "⚠️ Some favorites are no longer available.",
                   reply_markup=InlineKeyboardMarkup(
                       inline_keyboard=[
                           [InlineKeyboardButton(text="🛍 Products", callback_data="products_menu", style="primary")]]
                   ))
        return

    custom_prices = await asyncio.to_thread(
        _get_catalog_custom_prices, user_id, [product.id for product in products]
    )

    text = (
        f"⭐ <b>YOUR FAVORITES</b>\n\n"
        f"❤️ <b>{len(products)} favorite item(s):</b>\n\n"
        f"{_divider('─', 28)}\n\n"
    )

    for p in products:
        cat_config = _get_category_config(p.category)
        stock = _real_stock(p)
        stock_icon = "🟢" if stock > 0 else "🔴"
        is_free = float(p.price) == 0
        custom_price = custom_prices.get(p.id)
        price_text = "🎁 FREE" if is_free else f"💰 ${custom_price if custom_price is not None else _money(p.price):.2f}"
        if custom_price is not None:
            price_text += " 🏷 Custom"
        stock_text = "In Stock" if stock >= 999999 else str(stock)
        text += (
            f"{p.icon or cat_config['icon']} <b>{_esc(p.name)}</b>\n"
            f"  {price_text} | {stock_icon} Stock: {stock_text}\n"
            f"  🏷 {cat_config['color']} {cat_config['label']}\n\n"
        )

    keyboard = []
    for p in products:
        cat_config = _get_category_config(p.category)
        keyboard.append([
            InlineKeyboardButton(
                text=f"{p.icon or cat_config['icon']} {p.name}",
                callback_data=f"product_{p.id}",
                style=cat_config["style"]
            ),
            InlineKeyboardButton(
                text="💔 Remove",
                callback_data=f"toggle_fav_{p.id}",
                style="danger"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="success"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary"),
    ])

    await show(callback, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# ╔══════════════════════════════════════════════════════════════╗
# ║              NOTIFY WHEN AVAILABLE                           ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("notify_available_"))
async def notify_when_available(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    product = await asyncio.to_thread(_fetch_product, product_id)
    user_id = callback.from_user.id

    if not product:
        await callback.answer("Product is no longer available.", show_alert=True)
        return

    if _real_stock(product) > 0:
        await callback.answer("This product is already in stock.", show_alert=True)
        await product_info(callback, linked_product_id=product_id, acknowledge_callback=False)
        return

    is_new = await asyncio.to_thread(_set_restock_subscription, user_id, product_id, True)
    await callback.answer("Restock alert saved." if is_new else "You already have a restock alert.")
    cat_config = _get_category_config(product.category)

    if is_new:
        text = (
            f"🔔 <b>NOTIFICATION SET</b>\n\n"
            f"🔔 <b>You'll be notified when this is back in stock!</b>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"📦 <b>Product:</b> {product.icon or cat_config['icon']} {product.name}\n"
            f"🏷 <b>Category:</b> {cat_config['color']} {cat_config['label']}\n"
            f"💰 <b>Price:</b> ${_money(product.price):.2f}\n\n"
            f"📬 <b>What happens next:</b>\n"
            f"• You'll receive a Telegram message\n"
            f"• As soon as stock is available\n"
            f"• You'll be first to know!\n\n"
            f"<i>Stay tuned! 🎯</i>"
        )
    else:
        text = (
            f"🔔 <b>ALREADY SUBSCRIBED</b>\n\n"
            f"✅ <b>You're already on the notification list!</b>\n\n"
            f"📦 {product.icon or cat_config['icon']} <b>{product.name}</b>\n\n"
            f"We'll message you as soon as it's available.\n\n"
            f"<i>No need to subscribe again.</i>"
        )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔕 Unsubscribe", callback_data=f"notify_remove_{product_id}", style="danger")],
            [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="primary")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("notify_remove_"))
async def notify_remove(callback: CallbackQuery):
    product_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    removed = await asyncio.to_thread(_set_restock_subscription, user_id, product_id, False)
    await callback.answer(
        "🔕 Unsubscribed from notifications" if removed else "You weren't subscribed to this product",
        show_alert=True
    )
    # Refresh the alert card immediately so the removed item does not remain
    # visible as an active subscription until the next navigation.
    await restock_alerts_menu(callback, acknowledge_callback=False)


def _restock_subscribed_product_ids(user_id: int) -> list[int]:
    db = SessionLocal()
    try:
        return [row[0] for row in db.query(ProductEngagement.product_id).filter_by(
            telegram_id=user_id, restock_enabled=True
        ).order_by(ProductEngagement.created_at.desc()).all()]
    finally:
        db.close()


@router.callback_query(F.data == "restock_alerts_menu")
async def restock_alerts_menu(callback: CallbackQuery, acknowledge_callback: bool = True):
    if acknowledge_callback:
        await callback.answer()
    product_ids = await asyncio.to_thread(_restock_subscribed_product_ids, callback.from_user.id)
    products = await asyncio.to_thread(_fetch_products_by_ids, product_ids) if product_ids else []
    if not products:
        await show(
            callback,
            "🔔 <b>RESTOCK ALERTS</b>\n\nYou have no active alerts. Open an out-of-stock product and tap <b>Notify When Available</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛍 Browse products", callback_data="products_menu", style="success")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
            ]),
        )
        return

    keyboard = []
    lines = ["🔔 <b>YOUR RESTOCK ALERTS</b>\n", "We’ll message you when any of these products are restocked:\n"]
    for product in products[:40]:
        lines.append(f"• {_esc(product.icon or '📦')} <b>{_esc(product.name)}</b> · ${_money(product.price):.2f}")
        keyboard.append([
            InlineKeyboardButton(text=f"🛍 {product.name[:30]}", callback_data=f"product_{product.id}"),
            InlineKeyboardButton(text="🔕 Remove", callback_data=f"notify_remove_{product.id}", style="danger"),
        ])
    keyboard.append([InlineKeyboardButton(text="⬅ Shop", callback_data="products_menu", style="primary"),
                     InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")])
    await show(callback, "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


# ╔══════════════════════════════════════════════════════════════╗
# ║              SEARCH FUNCTIONALITY                            ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data == "search_start")
async def search_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SearchStates.waiting_query)

    text = (
        f"🔍 <b>SEARCH PRODUCTS</b>\n\n"
        f"<b>What are you looking for?</b>\n\n"
        f"{_divider('─', 28)}\n\n"
        f"💡 <b>Search tips:</b>\n"
        f"• Product name (e.g., Netflix)\n"
        f"• Category (e.g., VPN, Streaming)\n"
        f"• Keywords in description\n\n"
        f"<i>Type your search below 👇</i>\n"
        f"<i>Send 'cancel' to exit search</i>"
    )

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Browse All", callback_data="products_menu", style="primary"),
             InlineKeyboardButton(text="❌ Cancel", callback_data="search_cancel", style="danger")]
        ]
    )

    await show(callback, text, parse_mode="HTML", reply_markup=markup, state=state)


@router.callback_query(F.data == "search_cancel")
async def search_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await show(callback,
               f"❌ <b>SEARCH CANCELLED</b>\n\n<b>Search cancelled.</b>\n\nBrowse all products or try again later.",
               parse_mode="HTML",
               reply_markup=InlineKeyboardMarkup(
                   inline_keyboard=[
                       [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="success")],
                       [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
                   ]))


@router.message(SearchStates.waiting_query, F.text)
async def search_results(message: Message, state: FSMContext):
    query = message.text.strip()

    if query.lower() in ["cancel", "exit", "quit", "stop"]:
        await state.clear()
        data = await state.get_data()
        await update_card(message, None,
                          "<b>Search cancelled.</b>\n\nBrowse all products or try again later.",
                          chat_id=data.get("_card_chat_id"), message_id=data.get("_card_message_id"),
                          parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(
                              inline_keyboard=[[InlineKeyboardButton(text="🛍 Browse Products",
                                                                    callback_data="products_menu", style="success")]]
                          ))
        return

    if len(query) < 2:
        await update_card(message, state,
                          f"⚠️ <b>Search too short</b>\n\n{_divider('─', 24)}\n\nPlease enter at least 2 characters.\n\n<i>Try a product name or category.</i>",
                          parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(
                              inline_keyboard=[
                                  [InlineKeyboardButton(text="🛍 Browse All", callback_data="products_menu",
                                                        style="primary"),
                                   InlineKeyboardButton(text="❌ Cancel", callback_data="search_cancel", style="danger")]
                              ]))
        return

    await update_card(message, state,
                      f"🔍 <b>Searching for:</b> <code>{_esc(query)}</code>\n\n<i>Looking through products...</i>",
                      parse_mode="HTML")

    _schedule_reseller_stock_refresh()
    products = await asyncio.to_thread(_search_products, query)
    await state.clear()
    data = await state.get_data()
    card_chat_id = data.get("_card_chat_id")
    card_message_id = data.get("_card_message_id")

    if not products:
        text = (
            f"🔍 <b>NO RESULTS</b>\n\n"
            f"📭 <b>No products found for:</b>\n<code>{_esc(query)}</code>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"💡 <b>Suggestions:</b>\n• Try different keywords\n• Check spelling\n• Browse all categories\n\n"
            f"<i>Or browse all available products below.</i>"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛍 Browse All Products", callback_data="products_menu", style="success")],
                [InlineKeyboardButton(text="🔄 New Search", callback_data="search_start", style="primary"),
                 InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
            ])
    else:
        custom_prices = await asyncio.to_thread(
            _get_catalog_custom_prices, message.from_user.id, [product.id for product in products]
        )
        plural = "s" if len(products) != 1 else ""
        text = (
            f"🔍 <b>SEARCH RESULTS</b>\n\n"
            f"✅ <b>Found {len(products)} product{plural}</b>\nfor: <code>{_esc(query)}</code>\n\n"
            f"{_divider('─', 28)}\n\n"
        )

        for p in products[:MAX_SEARCH_RESULTS]:
            cat_config = _get_category_config(p.category)
            stock = _real_stock(p)
            stock_icon = "🟢" if stock > 0 else "🔴"
            is_free = float(p.price) == 0
            custom_price = custom_prices.get(p.id)
            price_text = "🎁 FREE" if is_free else f"💰 ${custom_price if custom_price is not None else _money(p.price):.2f}"
            if custom_price is not None:
                price_text += " 🏷 Custom"
            stock_text = "In Stock" if stock >= 999999 else str(stock)
            text += (
                f"{p.icon or cat_config['icon']} <b>{_esc(p.name)}</b>\n"
                f"  {price_text} | {stock_icon} Stock: {stock_text}\n"
                f"  🏷 {cat_config['color']} {cat_config['label']}\n\n"
            )

        if len(products) > 10:
            text += f"...and {len(products) - 10} more. Narrow your search or browse all.\n\n"

        text += f"{_divider('─', 28)}\n\n<i>Select a product from below to view details.</i>"

        keyboard = []
        for p in products[:10]:
            cat_config = _get_category_config(p.category)
            is_free = float(p.price) == 0
            custom_price = custom_prices.get(p.id)
            price_label = "FREE!" if is_free else f"${custom_price if custom_price is not None else _money(p.price):.2f}"
            if custom_price is not None:
                price_label += " 🏷"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{p.icon or cat_config['icon']} {p.name} — {price_label}",
                    callback_data=f"product_{p.id}",
                    style=cat_config["style"]
                )
            ])

        keyboard.append([
            InlineKeyboardButton(text="🔄 New Search", callback_data="search_start", style="primary"),
            InlineKeyboardButton(text="🛍 Show All", callback_data="products_menu", style="success")
        ])
        keyboard.append([
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")
        ])
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await update_card(message, None, text, chat_id=card_chat_id, message_id=card_message_id, parse_mode="HTML",
                      reply_markup=markup)


# ╔══════════════════════════════════════════════════════════════╗
# ║              PRODUCT DETAILS                                 ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("product_"))
async def product_info(
    callback: CallbackQuery | Message,
    linked_product_id: int | None = None,
    acknowledge_callback: bool = True,
):
    started = time.perf_counter()
    is_callback = isinstance(callback, CallbackQuery)
    if is_callback and acknowledge_callback:
        await callback.answer()
    product_id = linked_product_id if linked_product_id is not None else int(callback.data.split("_")[1])

    user_id = callback.from_user.id
    product, custom_price, is_fav = await asyncio.gather(
        asyncio.to_thread(_fetch_product, product_id),
        asyncio.to_thread(_get_custom_price, user_id, product_id),
        asyncio.to_thread(_is_favorite, user_id, product_id),
    )
    if product and (getattr(product, "source", "own") or "own") == "reseller":
        _schedule_reseller_stock_refresh()

    if not product:
        not_found_text = "❌ <b>NOT FOUND</b>\n\n<b>This product is no longer available.</b>"
        not_found_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="primary")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
        ])
        if is_callback:
            await show(callback, not_found_text, parse_mode="HTML", reply_markup=not_found_markup)
        else:
            await callback.answer(not_found_text, parse_mode="HTML", reply_markup=not_found_markup)
        return

    _fire_stock_scan(callback.bot, [product])

    max_qty = _get_max_qty(product)
    real_stock_available = _real_stock(product) > 0
    stock_count = _real_stock(product)
    cat_config = _get_category_config(product.category)

    delivery_type = (product.delivery_type or "automatic").lower()
    delivery_config = {
        "automatic": {"icon": "🤖", "label": "Auto-Delivery", "desc": "Instant delivery after purchase"},
        "manual": {"icon": "👨‍💼", "label": "Manual Delivery", "desc": "Delivered by our team"},
        "hybrid": {"icon": "🔀", "label": "Hybrid", "desc": "Auto + manual delivery"},
    }
    delivery_info = delivery_config.get(delivery_type, {"icon": "📦", "label": "Standard", "desc": ""})

    description_block = product.description.strip() if product.description else "<i>No description available.</i>"
    fav_star = "⭐" if is_fav else "☆"

    display_price = custom_price if custom_price is not None else _money(product.price)
    is_free = display_price == 0
    has_instruction = bool(product.delivery_instruction)

    text = (
        f"{product.icon or '📦'} <b>PRODUCT DETAILS</b>\n\n"
        f"<b>{product.icon or cat_config['icon']} {_esc(product.name)}</b> {fav_star}\n"
        f"🏷 {cat_config['color']} <b>{cat_config['label']}</b>\n\n"
        f"{_divider('─', 28)}\n\n"
        f"<b>📝 Description:</b>\n"
        f"<blockquote>{description_block}</blockquote>\n\n"
        f"{_divider('─', 28)}\n\n"
        f"<b>📊 Product Info:</b>\n"
        f"  🆔 <b>Product ID:</b> <code>{product.id}</code>\n"
    )

    if is_free:
        text += f"  🎁 <b>Price:</b> <code>FREE!</code>\n"
    else:
        label = "Custom Price" if custom_price is not None else "Base Price"
        text += f"  💰 <b>{label}:</b> <code>${display_price:.2f}</code> each\n"

    text += (
        f"  📦 <b>Stock:</b> {_stock_indicator(stock_count)}\n"
        f"  🏷 <b>Category:</b> {cat_config['icon']} {cat_config['label']}\n"
        f"  {delivery_info['icon']} <b>Delivery:</b> {delivery_info['label']}\n"
    )

    if policy_snapshot(product)['aftercare_policy'] != 'skip':
        text += f"  <b>{policy_label(product)}</b> after delivery\n"

    if delivery_info['desc']:
        text += f"  └ <i>{delivery_info['desc']}</i>\n"

    if (getattr(product, "source", "own") or "own") == "own" and manual_allowed(product):
        text += "  👨‍💼 Unavailable accounts are delivered by our team.\n"
    if product.preorder:
        text += f"  📦 <b>Preorder:</b> ✅ Available\n"

    if has_instruction:
        text += f"  📋 <b>Instructions:</b> ✅ Available (shown after purchase)\n"

    if not is_free:
        text += f"\n{_divider('─', 28)}\n"
        text += f"📦 <b>Bulk Pricing:</b>\n"
        if product.bulk_pricing:
            text += _format_bulk_pricing_text(product.bulk_pricing) + "\n"
            text += f"\n  💡 <i>Buy more, save more!</i>"
        else:
            text += f"  ❌ <i>Not Available</i>\n"
            text += f"  └ All quantities at base price"

    text += f"\n{_divider('═', 28)}\n"

    if not real_stock_available and max_qty > 0 and not is_free:
        text += f"\n⚠️ <b>Out of Stock — Preorder Available</b>\n<i>Order now and receive when restocked.</i>\n"

    buttons = []

    if max_qty <= 0:
        text += f"\n❌ <b>Currently Unavailable</b>"
        buttons.append([
            InlineKeyboardButton(text="🔔 Notify When Available", callback_data=f"notify_available_{product_id}",
                                 style="primary")
        ])
    else:
        if is_free:
            button_text = "🎁 Claim Free Product! 🎁" if real_stock_available else f"📦 Preorder (FREE)"
        else:
            button_text = f"🛒 Buy Now — ${display_price:.2f}" if real_stock_available else f"📦 Preorder — ${display_price:.2f}"
        button_style = "success" if real_stock_available else "primary"
        buttons.append([
            InlineKeyboardButton(text=button_text, callback_data=f"select_qty_{product_id}", style=button_style)
        ])
        buttons.append([
            InlineKeyboardButton(text="🛒 Add to cart",callback_data=f"ct_pick_{product_id}",style="primary"),
            InlineKeyboardButton(text="View cart",callback_data="ct_cart",style="primary"),
        ])
        if not real_stock_available:
            buttons.append([
                InlineKeyboardButton(
                    text="🔔 Notify When Available",
                    callback_data=f"notify_available_{product_id}",
                    style="primary",
                )
            ])
        if real_stock_available and max_qty >= 1 and not is_free:
            quick_buy_qty = f"{max_qty}" if max_qty < 999999 else "available"
            text += f"\n💡 <b>Quick Buy:</b> You can buy {quick_buy_qty} units."

    fav_label = "⭐ Remove from Favorites" if is_fav else "☆ Add to Favorites"
    fav_style = "danger" if is_fav else "success"
    buttons.append([
        InlineKeyboardButton(text=fav_label, callback_data=f"toggle_fav_{product_id}", style=fav_style),
    ])

    buttons.append([
        InlineKeyboardButton(text="🛍 All Products", callback_data="products_menu", style=cat_config["style"]),
        InlineKeyboardButton(text="📜 My Orders", callback_data="orders_menu", style="primary"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🔍 Search", callback_data="search_start", style="primary"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary"),
    ])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if is_callback:
        await show(callback, text, parse_mode="HTML", reply_markup=markup)
    else:
        await callback.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    logger.info("button_timing button=product user_id=%s product_id=%s total_ms=%.1f",
                user_id, product_id, (time.perf_counter() - started) * 1000)


# ╔══════════════════════════════════════════════════════════════╗
# ║             QUANTITY INPUT + CONFIRM                         ║
# ╚══════════════════════════════════════════════════════════════╝

def _qty_text(product, qty: int, real_stock_available: bool, custom_price: Decimal | None = None) -> str:
    is_free = float(product.price) == 0
    delivery_note = ""
    if (getattr(product, "source", "own") or "own") == "own":
        try:
            plan = plan_delivery(product, qty)
            delivery_note = f"✅ Ready now: {plan.automatic_quantity} · ⏳ Team delivery: {plan.manual_quantity}\n\n"
        except ValueError:
            delivery_note = "Stock will be checked again before purchase.\n\n"

    if is_free:
        cat_config = _get_category_config(product.category)
        header = "🎁 FREE CLAIM" if real_stock_available else "📦 PREORDER (FREE)"
        return (
            f"{product.icon or '🎁'} <b>{header}</b>\n\n"
            f"<b>{product.icon or cat_config['icon']} {product.name}</b>\n"
            f"🏷 {cat_config['color']} {cat_config['label']}\n\n"
            f"{_divider('─', 28)}\n\n"
            f"<b>📊 Order Summary:</b>\n"
            f"  🎁 <b>Price:</b> <code>FREE!</code>\n"
            f"  📦 <b>Quantity:</b> {qty}x\n"
            f"  ━━━━━━━━━━━━━━━━━━\n"
            f"  💵 <b>Total:</b> <code>$0.00</code>\n\n"
            f"{_divider('─', 28)}\n\n"
            f"{delivery_note}🎉 <b>No payment needed!</b>\n\n"
            f"<i>Confirm below to claim your free product.</i>"
        )

    actual_price = _money(custom_price if custom_price is not None else product.price)
    discount_note = ""

    if custom_price is None and product.bulk_pricing:
        bulk_price = _get_bulk_price(product.bulk_pricing, qty)
        if bulk_price is not None:
            actual_price = _money(bulk_price)
            if actual_price < _money(product.price):
                savings_per_unit = _money(product.price) - actual_price
                total_savings = savings_per_unit * qty
                discount_note = (
                    f"  🎉 <b>Bulk Discount Applied!</b>\n"
                    f"  └ 💰 ${actual_price:.2f}/each (saved ${total_savings:.2f} total!)\n"
                )

    total = actual_price * qty
    cat_config = _get_category_config(product.category)
    header = "📦 PREORDER" if not real_stock_available else "🛒 PURCHASE"
    return (
        f"{product.icon or '📦'} <b>{header}</b>\n\n"
        f"<b>{product.icon or cat_config['icon']} {product.name}</b>\n"
        f"🏷 {cat_config['color']} {cat_config['label']}\n\n"
        f"{_divider('─', 28)}\n\n"
        f"<b>📊 Order Summary:</b>\n"
        f"  📦 <b>Quantity:</b> {qty}x\n"
        f"  💰 <b>Price per unit:</b> ${actual_price:.2f}\n"
        f"{discount_note}"
        f"  ━━━━━━━━━━━━━━━━━━\n"
        f"  💵 <b>Total:</b> <code>${total:.2f}</code>\n\n"
        f"{_divider('─', 28)}\n\n"
        f"{delivery_note}<i>Please confirm your order below.</i>"
    )


def _confirm_keyboard(product_id: int, is_free: bool = False, checkout_token: str | None = None) -> InlineKeyboardMarkup:
    confirm_text = "🎁 Claim Now! 🎁" if is_free else "✅ Confirm Purchase"
    buttons = [[InlineKeyboardButton(text=confirm_text, callback_data=f"confirm_buy_{product_id}_{checkout_token or 'expired'}", style="success")]]

    buttons.append([InlineKeyboardButton(text="🛒 Add to cart", callback_data=f"ct_add_{product_id}", style="primary"), InlineKeyboardButton(text="View cart", callback_data="ct_cart")])
    if not is_free:
        buttons.append(
            [InlineKeyboardButton(text="🔄 Change Quantity", callback_data=f"select_qty_{product_id}", style="primary"),
             InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_buy_{product_id}", style="danger")])
    else:
        buttons.append(
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_buy_{product_id}", style="danger")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _cancel_input_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_buy_{product_id}", style="danger"),
             InlineKeyboardButton(text="🔙 Back to Product", callback_data=f"product_{product_id}", style="primary")]
        ]
    )


@router.callback_query(F.data.startswith("select_qty_"))
async def select_qty(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    product_id = int(callback.data.split("_")[2])

    _schedule_reseller_stock_refresh()
    product, custom_price = await asyncio.gather(
        asyncio.to_thread(_fetch_product, product_id),
        asyncio.to_thread(_get_custom_price, callback.from_user.id, product_id),
    )

    if not product or not product.is_active:
        await show(callback, f"⚠️ <b>Product Unavailable</b>", parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="primary")]
                   ]))
        return

    max_qty = _get_max_qty(product)
    if max_qty <= 0:
        await show(callback,
                   f"❌ <b>OUT OF STOCK</b>\n\n<b>This product is currently unavailable.</b>",
                   parse_mode="HTML",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="🛍 Browse Other Products", callback_data="products_menu",
                                            style="success")],
                       [InlineKeyboardButton(text="🔔 Notify When Available",
                                            callback_data=f"notify_available_{product_id}", style="primary")]
                   ]))
        return

    is_free = float(product.price) == 0

    if is_free:
        await state.update_data(**{f"qty_{product_id}": 1})
        token = await confirmation_token(state, product_id)
        real_stock_available = _real_stock(product) > 0
        await show(callback, _qty_text(product, 1, real_stock_available, custom_price),
                   parse_mode="HTML", reply_markup=_confirm_keyboard(product_id, is_free=True, checkout_token=token))
        return

    cat_config = _get_category_config(product.category)
    range_text = "1" if max_qty == 1 else (f"1 to {max_qty}" if max_qty < 999999 else "1 or more")

    stock_disp = "In Stock" if max_qty >= 999999 else f"{max_qty} units"

    has_bulk = bool(product.bulk_pricing) and custom_price is None
    bulk_info = ""
    if has_bulk:
        bulk_info = "\n📦 <b>💰 Bulk Discounts Available!</b>\n"
        bulk_tiers = _format_bulk_pricing_text(product.bulk_pricing)
        bulk_info += bulk_tiers
        bulk_info += "\n\n<i>The price adjusts automatically based on your quantity!</i>"

    text = (
        f"🔢 <b>SELECT QUANTITY</b>\n\n"
        f"<b>{product.icon or cat_config['icon']} {product.name}</b>\n"
        f"💰 <b>{'Custom' if custom_price is not None else 'Base'} Price:</b> ${_money(custom_price if custom_price is not None else product.price):.2f} each\n"
        f"📦 <b>Available:</b> {stock_disp}\n"
        f"{bulk_info}\n"
        f"{_divider('─', 28)}\n\n"
        f"🔢 <b>How many would you like?</b>\n\n"
        f"Reply with a number ({range_text})"
    )

    text += f"\n\n<i>Example: Send 3 for three units</i>"

    await state.set_state(PurchaseStates.waiting_qty)
    await state.update_data(pending_product_id=product_id)
    await show(callback, text, parse_mode="HTML", reply_markup=_cancel_input_keyboard(product_id), state=state)


@router.message(PurchaseStates.waiting_qty, F.text)
async def receive_qty(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = data.get("pending_product_id")
    card_chat_id = data.get("_card_chat_id")
    card_message_id = data.get("_card_message_id")

    if is_cancel(message.text):
        await state.clear()
        await update_card(message, None, 'Cancelled. No order was placed.', chat_id=card_chat_id,
                          message_id=card_message_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text='Back to product' if product_id else 'Products',
                                                   callback_data=f'product_{product_id}' if product_id else 'products_menu'),
                              InlineKeyboardButton(text='Home', callback_data='main_menu')]]))
        return

    if product_id is None:
        await state.clear()
        await update_card(message, None, f"⚠️ <b>Session Expired</b>\n\nPlease start over from the product page.",
                          chat_id=card_chat_id, message_id=card_message_id, parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="🛍 Products", callback_data="products_menu", style="primary")]
                          ]))
        return

    _schedule_reseller_stock_refresh()
    product, custom_price = await asyncio.gather(
        asyncio.to_thread(_fetch_product, product_id),
        asyncio.to_thread(_get_custom_price, message.from_user.id, product_id),
    )

    if not product or not product.is_active:
        await state.clear()
        await update_card(message, None, f"⚠️ <b>Product Unavailable</b>",
                          chat_id=card_chat_id, message_id=card_message_id, parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="🛍 Products", callback_data="products_menu", style="primary")]
                          ]))
        return

    is_free = float(product.price) == 0
    if is_free:
        await state.update_data(**{f"qty_{product_id}": 1})
        token = await confirmation_token(state, product_id)
        real_stock_available = _real_stock(product) > 0
        await update_card(message, state, _qty_text(product, 1, real_stock_available, custom_price),
                          parse_mode="HTML", reply_markup=_confirm_keyboard(product_id, is_free=True, checkout_token=token))
        return

    max_qty = _get_max_qty(product)
    if max_qty <= 0:
        await state.clear()
        await update_card(message, None, f"❌ <b>Out of Stock</b>",
                          chat_id=card_chat_id, message_id=card_message_id, parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                              [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu",
                                                   style="primary")]
                          ]))
        return

    try:
        qty = parse_quantity(message.text, max_qty)
    except InputError as error:
        await update_card(message, state, str(error), reply_markup=_cancel_input_keyboard(product_id))
        return

    await state.update_data(**{f"qty_{product_id}": qty})
    token = await confirmation_token(state, product_id)
    real_stock_available = _real_stock(product) > 0
    await update_card(message, state, _qty_text(product, qty, real_stock_available, custom_price),
                      parse_mode="HTML", reply_markup=_confirm_keyboard(product_id, is_free, checkout_token=token))


@router.callback_query(F.data.startswith("cancel_buy_"))
async def cancel_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    product_id = int(callback.data.split("_")[2])
    await state.update_data(**{f"qty_{product_id}": 1})
    await state.clear()
    await show(callback,
               f"❌ <b>CANCELLED</b>\n\n<b>Purchase cancelled.</b>\n\nYour balance has not been charged.",
               parse_mode="HTML",
               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                   [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="success"),
                    InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
               ]))


# ╔══════════════════════════════════════════════════════════════╗
# ║             OWN PRODUCT PURCHASE TRANSACTION                 ║
# ╚══════════════════════════════════════════════════════════════╝

@retry_on_write_conflict(max_attempts=3)
def _do_purchase(
    telegram_id: int,
    product_id: int,
    quantity: int,
    delivery_telegram_id: int | None = None,
    checkout_key: str | None = None,
    expected_total: str | None = None,
) -> dict:
    with transaction() as db:
        user = db.query(User).filter(User.telegram_id == telegram_id).with_for_update().first()
        if not user:
            return {"error": "User not found."}
        if getattr(user, "is_banned", False):
            return {"error": "Your account is banned from making purchases."}

        if checkout_key:
            existing = db.query(Order).filter(Order.checkout_key == checkout_key, Order.telegram_id == telegram_id).first()
            if existing:
                return {"duplicate_order_id": existing.id}

        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product:
            return {"error": "Product not found."}
        if not product.is_active:
            return {"error": "Product unavailable."}

        if float(product.price) == 0:
            quantity = 1
            already_claimed = (
                                      db.query(Order)
                                      .filter(
                                          Order.telegram_id == telegram_id,
                                          Order.product_id == product.id,
                                          Order.status.in_(["completed", "pending_info", "pending_manual", "preorder"]),
                                          Order.refunded == False
                                      )
                                      .count()
                              ) > 0
            if already_claimed:
                return {
                    "error": (
                        "🎁 <b>Already Claimed!</b>\n\n"
                        "You've already claimed this free product.\n"
                        "Each freebie can only be claimed once per user.\n\n"
                        "💡 Check out our other free products or browse paid items!"
                    )
                }

        if quantity < 1:
            return {"error": "Quantity must be at least 1."}

        if delivery_telegram_id is not None and delivery_telegram_id <= 0:
            return {"error": "delivery_telegram_id must be a positive Telegram user ID."}

        # Final checkout reads the live rule, so a stopped rate cannot be
        # used from an already-open product or confirmation screen.
        custom_price = _active_custom_price(db, telegram_id, product.id)
        price = custom_price if custom_price is not None else _money(product.price)
        if custom_price is None and product.bulk_pricing:
            bulk_price = _get_bulk_price(product.bulk_pricing, quantity)
            if bulk_price is not None:
                price = _money(bulk_price)

        total_amount = _money(price * quantity)
        if expected_total is not None and total_amount != _money(expected_total):
            return {"error": "Price changed. Review your cart again. Customer not charged."}
        user_balance = _money(user.balance)

        if user_balance < total_amount:
            return {
                "error": "insufficient_balance",
                "total_price": float(total_amount),
                "balance": float(user_balance)
            }

        delivery_type = (product.delivery_type or "automatic").lower()
        if delivery_telegram_id is not None and not manual_allowed(product):
            return {
                "error": (
                    "delivery_telegram_id can be used only with a product "
                    "with manual delivery or manual fallback enabled."
                )
            }
        threshold = product.low_stock_threshold if product.low_stock_threshold is not None else DEFAULT_LOW_STOCK_THRESHOLD

        try:
            plan = plan_delivery(product, quantity)
        except ValueError as exc:
            return {"error": str(exc)}
        stock_before = order_capacity(product)
        delivered_accounts = list(plan.automatic)
        reserved_delivery = []
        required_info = product.required_info if isinstance(product.required_info, list) else []
        is_preorder_order = plan.preorder
        new_stock = plan.stock_after
        if not plan.preorder:
            product.file_content = "\n".join(plan.remaining) or None
            if plan.reusable_quantity:
                product.reusable_stock = max(0, int(product.reusable_stock or 0) - plan.reusable_quantity)
            product.stock = new_stock
            # Once the last auto-deliverable unit is consumed, route future
            # purchases to the manual queue instead of leaving the product
            # labelled automatic with no deliverable inventory.
            if (
                delivery_type in {"automatic", "hybrid"}
                and not any(line.strip() for line in str(product.file_content or "").splitlines())
                and not (
                    str(getattr(product, "reusable_delivery", "") or "").strip()
                    and int(getattr(product, "reusable_stock", 0) or 0) > 0
                )
            ):
                product.delivery_type = "manual"
                product.manual_fallback = True
                product.preorder = False
        if required_info:
            reserved_delivery = delivered_accounts
            delivered_accounts = []
        status = "pending_info" if required_info else (
            "preorder" if plan.preorder else "pending_manual" if plan.manual_quantity else "completed"
        )

        user.balance = user_balance - total_amount
        user.total_orders += 1
        user.total_spent = _money(user.total_spent) + total_amount

        order = Order(
            telegram_id=user.telegram_id, product_id=product.id, product_name=product.name,
            delivered_account="\n".join(delivered_accounts) if delivered_accounts else None,
            reserved_delivery="\n".join(reserved_delivery) if reserved_delivery else None,
            reusable_delivery_account=plan.reusable_content,
            reusable_delivery_quantity=plan.reusable_quantity,
            required_info_snapshot=[dict(item) for item in required_info if isinstance(item, dict)] or None,
            amount=total_amount, quantity=quantity, delivery_type=plan.order_mode,
            automatic_quantity=plan.automatic_quantity, manual_pending_quantity=plan.manual_quantity,
            stock_deducted_quantity=0 if plan.preorder else quantity,
            is_preorder=is_preorder_order, status=status, refunded=False,
            delivery_telegram_id=delivery_telegram_id, checkout_key=checkout_key,
        )
        for field, value in policy_snapshot(product).items():
            setattr(order, field, value)
        if status == "completed":
            start_coverage(order)
        db.add(order)
        db.flush()
        record_event(db, order.id, 'created', 'Order received.', actor_id=telegram_id)
        if status == 'pending_info':
            record_event(db, order.id, 'awaiting_information', 'Waiting for customer information.')
        elif plan.automatic:
            record_event(db, order.id, 'automatic_delivery', f'{plan.automatic_quantity} unit(s) delivered automatically.')
        if status == 'completed':
            record_event(db, order.id, 'completed', 'Order completed.')
        elif status == 'pending_manual':
            record_event(db, order.id, 'manual_pending', f'{plan.manual_quantity} unit(s) awaiting team delivery.')

        if status == "pending_info":
            enqueue(db, f"required-info:{order.id}", user.telegram_id,
                    "Your order needs the requested information before delivery. Open Notifications and tap Submit required information.",
                    source_order_id=order.id)
        elif status == "completed":
            enqueue(db, f"order-delivery:{order.id}", user.telegram_id,
                    "Your order has been delivered. Open My Orders to view the delivery details.",
                    source_order_id=order.id)
        elif status == "pending_manual":
            enqueue(db, f"order-update:{order.id}:manual-pending", user.telegram_id,
                    "Your order is confirmed and waiting for manual fulfillment. Open My Orders to check its status.",
                    source_order_id=order.id)
        elif status == "preorder":
            enqueue(db, f"order-update:{order.id}:preorder", user.telegram_id,
                    "Your preorder is confirmed and is waiting for stock. Open My Orders to check its status.",
                    source_order_id=order.id)

        referral_commission_paid = None
        if not is_preorder_order and user.referred_by:
            order.referral_commission_amount = Decimal("0")
            order.referral_commission_to_balance = bool(REFERRAL_CREDIT_TO_BALANCE)
            referrer = db.query(User).filter(User.telegram_id == user.referred_by).with_for_update().first()
            if referrer is not None:
                commission = _money(total_amount * REFERRAL_COMMISSION_RATE)
                if commission > 0:
                    referrer.referral_earnings = _money(referrer.referral_earnings) + commission
                    order.referral_commission_amount = commission
                    order.referral_commission_to_balance = bool(REFERRAL_CREDIT_TO_BALANCE)
                    if REFERRAL_CREDIT_TO_BALANCE:
                        referrer.balance = _money(referrer.balance) + commission
                    referral_commission_paid = {"referrer_telegram_id": referrer.telegram_id, "amount": commission}

        low_stock_alert = None
        if not is_preorder_order and stock_before > threshold >= new_stock:
            low_stock_alert = {"product_id": product.id, "product_name": product.name, "stock": new_stock,
                               "threshold": threshold}

        result = {
            "order_id": order.id, "product_id": product.id, "icon": product.icon, "name": product.name,
            "delivered_accounts": delivered_accounts, "balance": user.balance,
            "stock": new_stock, "status": status, "is_preorder": is_preorder_order,
            "quantity": quantity, "total_price": total_amount, "price_per_unit": price,
            "low_stock_alert": low_stock_alert, "referral_commission_paid": referral_commission_paid,
            "delivery_instruction": product.delivery_instruction,
            "required_info": required_info,
            "automatic_quantity": plan.automatic_quantity, "manual_pending_quantity": plan.manual_quantity,
        }
    invalidate_products_cache()
    return result


# ╔══════════════════════════════════════════════════════════════╗
# ║          RESELLER PURCHASE TRANSACTION (FINANCIAL SAFETY)    ║
# ╚══════════════════════════════════════════════════════════════╝

async def _do_reseller_purchase(
    telegram_id: int,
    product_id: int,
    quantity: int,
    progress_callback=None,
    checkout_key: str | None = None,
    expected_total: str | None = None,
) -> dict:
    """Serialize purchases for one supplier service within this bot process."""
    def _lock_identity():
        db = SessionLocal()
        try:
            product = db.query(Product.provider_id, Product.reseller_name, Product.reseller_service_id).filter(
                Product.id == product_id
            ).first()
            if not product:
                return "missing", str(product_id)
            provider_id, reseller_name, service_id = product
            return str(provider_id or reseller_name or "default_provider").casefold(), str(service_id or "").strip()
        finally:
            db.close()

    identity = await asyncio.to_thread(_lock_identity)
    lock = _supplier_purchase_locks.setdefault(identity, asyncio.Lock())
    async with lock:
        return await _do_reseller_purchase_unlocked(
            telegram_id,
            product_id,
            quantity,
            progress_callback=progress_callback,
            checkout_key=checkout_key,
            expected_total=expected_total,
        )


async def _do_reseller_purchase_unlocked(
    telegram_id: int,
    product_id: int,
    quantity: int,
    progress_callback=None,
    checkout_key: str | None = None,
    expected_total: str | None = None,
) -> dict:
    async def report_progress(percent: int, stage: str) -> None:
        if progress_callback is None:
            return
        try:
            await progress_callback(percent, stage)
        except Exception:
            logger.warning("Could not update reseller purchase progress", exc_info=True)

    if not ResellerManager:
        return {"error": "Reseller integration module is unavailable."}

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product or not product.is_active:
            return {"error": "Product is no longer available."}

        if float(product.price) == 0:
            quantity = 1

        if getattr(product, "source", "own") != "reseller":
            return {"error": "Invalid product source."}

        service_id = str(getattr(product, "reseller_service_id", "") or "").strip()
        if not service_id:
            return {"error": "Product configuration error (missing reseller service ID). Customer not charged."}

        await _refresh_reseller_stock_cache_if_needed()
        db_stock = getattr(product, "stock", 0)
        fallback_stk = db_stock if db_stock is not None else 999999
        provider_scope = getattr(product, "provider_id", None) or getattr(product, "reseller_name", None)
        live_stock, stock_is_stale = _cached_reseller_stock(provider_scope, service_id)
        if stock_is_stale:
            return {"error": "Supplier stock could not be refreshed safely. Please try again shortly."}
        if live_stock is None:
            return {"error": "Supplier stock is not confirmed yet. Please try again shortly."}
        # Do not cap fresh supplier stock with the import-time local snapshot.
        available_stock = live_stock

        if available_stock < quantity:
            return {"error": f"Only {available_stock} left in stock with supplier."}

        custom_price = _active_custom_price(db, telegram_id, product.id)
        price = custom_price if custom_price is not None else _money(product.price)
        if custom_price is None and product.bulk_pricing:
            bulk_price = _get_bulk_price(product.bulk_pricing, quantity)
            if bulk_price is not None:
                price = _money(bulk_price)

        total_amount = _money(price * quantity)
        if expected_total is not None and total_amount != _money(expected_total):
            return {"error": "Price changed. Review your cart again. Customer not charged."}

        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return {"error": "User not found."}
        if getattr(user, "is_banned", False):
            return {"error": "Your account is banned from making purchases."}

        user_balance = _money(user.balance)
        if user_balance < total_amount:
            return {
                "error": "insufficient_balance",
                "total_price": float(total_amount),
                "balance": float(user_balance)
            }

        saved_aftercare = policy_snapshot(product)
        product_name = product.name
        product_icon = product.icon
        delivery_instruction = product.delivery_instruction
        prov_id = getattr(product, "provider_id", None) or getattr(product, "reseller_name", None)
    finally:
        db.close()

    creds = _get_reseller_credentials(prov_id)
    api_key = creds["api_key"]
    base_url = creds["base_url"]

    if not api_key or not base_url:
        return {"error": "Reseller service is not configured properly."}

    await report_progress(30, "Checking your balance and product availability")
    external_order_id = f"RD-{checkout_key[:32]}" if checkout_key else f"ORD-{telegram_id}-{int(time.time())}"

    try:
        manager = ResellerManager(api_key=api_key, base_url=base_url, provider_config=creds)
        await report_progress(60, "Placing your order with the supplier")
        api_response = await _call_reseller_place_order(
            manager,
            service_id=service_id,
            quantity=quantity,
            external_order_id=external_order_id
        )
    except ResellerAPIError as e:
        status_code = getattr(e, "status_code", 0)
        err_detail = str(e)
        logger.error("ResellerAPIError during purchase (status %s):\n%s", status_code, err_detail)

        return {"error": f"❌ Provider Error ({status_code}): {err_detail}", "needs_review": not status_code or status_code >= 500}
    except Exception as e:
        logger.exception("Unexpected error ordering from reseller API")
        return {"error": f"❌ Supplier service error: {str(e)}", "needs_review": True}

    delivered_list = []
    delivery_items_are_atomic = False
    if isinstance(api_response, dict):
        raw_products = (
                api_response.get("delivery_items") or
                api_response.get("delivery") or
                api_response.get("products") or
                api_response.get("code") or
                api_response.get("account") or
                api_response.get("accounts") or
                api_response.get("data") or
                api_response.get("credentials") or
                api_response.get("result") or
                api_response.get("delivered_account") or
                api_response.get("items")
        )
        if raw_products is None and api_response.get("fulfillment") is not None:
            fulfillment = api_response.get("fulfillment")
            if isinstance(fulfillment, dict):
                for key in ("payload", "delivery_data", "delivery", "delivery_items",
                            "accounts", "credentials", "code", "result"):
                    if fulfillment.get(key) is not None:
                        raw_products = fulfillment[key]
                        break
            else:
                raw_products = fulfillment
        if isinstance(raw_products, str):
            delivered_list = [raw_products]
        elif isinstance(raw_products, list):
            delivered_list = [str(p) for p in raw_products if p]
            delivery_items_are_atomic = True
        elif isinstance(raw_products, dict):
            delivered_list = [json.dumps(raw_products, ensure_ascii=False)]
            delivery_items_are_atomic = True
        elif isinstance(raw_products, (int, float)) and not isinstance(raw_products, bool):
            delivered_list = [str(raw_products)]
            delivery_items_are_atomic = True
    elif isinstance(api_response, list):
        delivered_list = [str(p) for p in api_response if p]
        delivery_items_are_atomic = True
    elif isinstance(api_response, str):
        delivered_list = [api_response]

    is_async_provider_order = bool(
        getattr(manager, "is_hmac_auth", False)
        and isinstance(api_response, dict)
        and api_response.get("order_id")
        and not delivered_list
    )
    if not delivered_list and not is_async_provider_order:
        response_fields = sorted(api_response.keys()) if isinstance(api_response, dict) else []
        logger.error(
            "Reseller API returned success without product content response_type=%s fields=%s",
            type(api_response).__name__, response_fields,
        )
        return {"error": "❌ Supplier returned no product codes. Your balance was not charged."}

    delivered_units = (
        len(delivered_list) if delivery_items_are_atomic
        else _supplier_delivery_units(delivered_list)
    )
    expected_units = max(1, int(quantity or 1))
    partial_delivery = bool(delivered_list and delivered_units < expected_units)
    if delivered_list and delivered_units > expected_units:
        logger.warning(
            "Supplier returned extra delivery units; limiting user delivery order_id=%s expected=%s got=%s",
            external_order_id, expected_units, delivered_units,
        )
        remaining_units = expected_units
        limited_items = []
        for item in delivered_list:
            lines = [line for line in str(item).splitlines() if line.strip()]
            take = 1 if delivery_items_are_atomic else min(len(lines), remaining_units)
            limited_items.append(item if delivery_items_are_atomic else "\n".join(lines[:take]))
            remaining_units -= take
            if remaining_units <= 0:
                break
        delivered_list = limited_items
        delivered_units = len(delivered_list) if delivery_items_are_atomic else _supplier_delivery_units(delivered_list)

    await report_progress(80, "Supplier confirmed; preparing your delivery")
    provider_order_id = None
    if isinstance(api_response, dict):
        provider_order_id = api_response.get("order_id") or api_response.get("order_no")
    local_provider_id = int(creds["id"]) if str(creds.get("id", "")).isdigit() else None

    @retry_on_write_conflict(max_attempts=3)
    def _finalize_db_transaction():
        with transaction() as db:
            u = db.query(User).filter(User.telegram_id == telegram_id).with_for_update().first()
            if not u:
                raise Exception("User not found during transaction commit")

            if checkout_key:
                existing = db.query(Order).filter(Order.checkout_key == checkout_key, Order.telegram_id == telegram_id).first()
                if existing:
                    return {"duplicate_order_id": existing.id}
            if _money(u.balance) < total_amount:
                raise RuntimeError('Wallet changed after supplier confirmation; manual reconciliation required')

            previous_balance = _money(u.balance)
            u.balance = previous_balance - total_amount
            u.total_orders += 1
            u.total_spent = _money(u.total_spent) + total_amount

            purchased_product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
            if purchased_product is not None and purchased_product.stock is not None and purchased_product.stock < 999999:
                purchased_product.stock = max(0, purchased_product.stock - quantity)

            delivered_text = "\n".join(delivered_list) if delivered_list else None
            order_status = (
                "processing" if is_async_provider_order else
                "pending_manual" if partial_delivery else "completed"
            )
            order_delivery_type = "hybrid" if partial_delivery else "automatic"
            manual_pending_quantity = max(0, quantity - delivered_units) if partial_delivery else 0

            order = Order(
                telegram_id=u.telegram_id,
                product_id=product_id,
                product_name=product_name,
                delivered_account=delivered_text,
                amount=total_amount,
                quantity=quantity,
                delivery_type=order_delivery_type,
                automatic_quantity=delivered_units,
                manual_pending_quantity=manual_pending_quantity,
                is_preorder=False,
                status=order_status,
                refunded=False, checkout_key=checkout_key,
                reseller_id=local_provider_id,
                reseller_service_id=service_id,
                reseller_order_id=external_order_id if is_async_provider_order else str(provider_order_id or external_order_id),
                reseller_provider_order_id=str(provider_order_id) if is_async_provider_order and provider_order_id else None,
            )
            db.add(order)
            db.flush()
            for field, value in saved_aftercare.items():
                setattr(order, field, value)
            if order.status == "completed":
                start_coverage(order)
            record_event(db, order.id, 'created', 'Supplier order received.', actor_id=telegram_id)
            event_kind = 'supplier_pending' if is_async_provider_order else 'supplier_partial' if partial_delivery else 'completed'
            event_summary = (
                'Supplier is processing the order.' if is_async_provider_order else
                f'Supplier delivered {delivered_units} of {quantity} units; the team will deliver the remainder.'
                if partial_delivery else 'Supplier delivery completed.'
            )
            record_event(db, order.id, event_kind, event_summary)

            enqueue(db, f"order-update:{order.id}:supplier", u.telegram_id,
                    "Your supplier order is being processed. Open My Orders to check its status." if is_async_provider_order
                    else f"Your supplier delivered {delivered_units} of {quantity} units. The team will deliver the remaining {manual_pending_quantity}. Open My Orders to track it." if partial_delivery
                    else "Your order has been delivered. Open My Orders to view the delivery details.",
                    source_order_id=order.id)


            referral_commission_paid = None
            if u.referred_by:
                order.referral_commission_amount = Decimal("0")
                order.referral_commission_to_balance = bool(REFERRAL_CREDIT_TO_BALANCE)
                referrer = db.query(User).filter(User.telegram_id == u.referred_by).with_for_update().first()
                if referrer is not None:
                    commission = _money(total_amount * REFERRAL_COMMISSION_RATE)
                    if commission > 0:
                        referrer.referral_earnings = _money(referrer.referral_earnings) + commission
                        order.referral_commission_amount = commission
                        order.referral_commission_to_balance = bool(REFERRAL_CREDIT_TO_BALANCE)
                        if REFERRAL_CREDIT_TO_BALANCE:
                            referrer.balance = _money(referrer.balance) + commission
                        referral_commission_paid = {"referrer_telegram_id": referrer.telegram_id, "amount": commission}

            stock_left = _cached_reseller_stock(provider_scope, service_id)[0]
            if stock_left is None:
                stock_left = fallback_stk
            stock_disp = "In Stock" if stock_left >= 999999 else stock_left

            return {
                "order_id": order.id,
                "product_id": product_id,
                "icon": product_icon,
                "name": product_name,
                "delivered_accounts": delivered_list,
                "balance": u.balance,
                "stock": stock_disp,
                "status": order_status,
                "is_preorder": False,
                "quantity": quantity,
                "automatic_quantity": delivered_units,
                "manual_pending_quantity": manual_pending_quantity,
                "total_price": total_amount,
                "price_per_unit": price,
                "low_stock_alert": None,
                "referral_commission_paid": referral_commission_paid,
                "delivery_instruction": delivery_instruction,
            }

    try:
        await report_progress(95, "Securing your order and updating stock")
        result = await asyncio.to_thread(_finalize_db_transaction)
        if result.get("duplicate_order_id"):
            return result
        cache_key = _reseller_cache_key(provider_scope, service_id)
        if cache_key in _reseller_stock_cache and _reseller_stock_cache[cache_key] < 999999:
            _reseller_stock_cache[cache_key] = max(0, _reseller_stock_cache[cache_key] - quantity)
            _reseller_stock_cache_updated[cache_key] = time.monotonic()
            result["stock"] = min(
                _reseller_stock_cache[cache_key],
                result["stock"] if isinstance(result["stock"], int) else _reseller_stock_cache[cache_key],
            )
        invalidate_products_cache()
        return result
    except Exception:
        logger.exception("Error finalizing local order after successful reseller order")
        try:
            def _queue_reconciliation_alert():
                from services.notification_outbox import enqueue_admin
                with transaction() as db:
                    enqueue_admin(
                        db,
                        f"supplier-finalize:{checkout_key or external_order_id}",
                        "A supplier accepted an order, but RainDeals could not save its local order record. "
                        f"Check supplier reference {external_order_id}, provider {prov_id}, service {service_id}, "
                        f"buyer {telegram_id}, and checkout {checkout_key or 'unavailable'} before retrying."
                    )
            await asyncio.to_thread(_queue_reconciliation_alert)
        except Exception:
            logger.exception("Could not queue supplier finalization review alert reference=%s", external_order_id)
        return {"error": "❌ Order processing needs review. Check My Orders and contact support before retrying.", "needs_review": True}


async def _notify_admins_low_stock(bot, alert: dict, order_id: int):
    from services.notification_outbox import admin_destination
    for admin_id in filter(None, [admin_destination()]):
        try:
            await asyncio.to_thread(enqueue_once, f"low-stock:{order_id}", admin_id,
                                   f"⚠️ <b>LOW STOCK ALERT</b>\n\n{_divider('─', 24)}\n\n"
                                   f"📦 <b>Product:</b> {alert['product_name']}\n🆔 <b>ID:</b> #{alert['product_id']}\n"
                                   f"📊 <b>Remaining:</b> {alert['stock']}\n🔔 <b>Threshold:</b> {alert['threshold']}\n\n"
                                   f"⚡ <i>Stock has dropped below the alert threshold.</i>", bot.id)
        except Exception:
            logger.exception("Failed to notify admin %s of low stock", admin_id)


async def _notify_stock_purchase(bot, buyer_id: int, result: dict):
    """Announce a completed purchase without exposing buyer/order details."""
    stock = result.get("stock")
    has_stock = stock == "In Stock" or (isinstance(stock, (int, float)) and stock > 0)
    await _send_stock_channel_message(
        bot,
        f"🛍 <b>PRODUCT PURCHASED</b>\n\n"
        f"📦 <b>{_esc(result['name'])}</b>\n"
        f"➖ Quantity sold: <b>{result['quantity']}x</b>\n"
        f"📊 Remaining stock: <b>{result.get('stock', 'Unknown')}</b>",
        product_id=result.get("product_id"),
        can_buy=has_stock,
    )


async def _notify_referral_commission(bot, commission: dict, order_id: int):
    try:
        await asyncio.to_thread(enqueue_once, f"referral:{order_id}",
            commission["referrer_telegram_id"],
            f"🎉 <b>Referral Commission Earned!</b>\n\n{_divider('─', 22)}\n\n"
            f"💵 <b>Amount:</b> ${commission['amount']:.2f}\n👤 <b>From:</b> A user you referred\n\n"
            f"<i>Thanks for sharing your link! 🙏</i>",
            bot.id,
        )
    except Exception:
        logger.exception("Failed to notify referrer %s", commission.get("referrer_telegram_id"))


# ╔══════════════════════════════════════════════════════════════╗
# ║          DELIVERY INSTRUCTION BUTTON HANDLER                 ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.startswith("delivery_instruction_"))
async def show_delivery_instruction(callback: CallbackQuery):
    await callback.answer()
    product_id = int(callback.data.split("_")[2])
    product = await asyncio.to_thread(_fetch_product, product_id)

    if not product or not product.delivery_instruction:
        await callback.answer("📋 No delivery instructions available.", show_alert=True)
        return

    cat_config = _get_category_config(product.category)

    text = (
        f"╔{'═' * 30}╗\n"
        f"║  📋 DELIVERY INSTRUCTIONS       ║\n"
        f"╚{'═' * 30}╝\n\n"
        f"<b>{product.icon or cat_config['icon']} {_esc(product.name)}</b>\n\n"
        f"{'─' * 30}\n\n"
        f"<b>⚠️ IMPORTANT — READ CAREFULLY:</b>\n\n"
        f"<blockquote>{_esc(product.delivery_instruction)}</blockquote>\n\n"
        f"{'─' * 30}\n\n"
        f"<i>💡 Please follow these instructions carefully\n"
        f"to ensure a smooth experience.</i>\n\n"
        f"<i>If you have any issues, contact support!</i>"
    )

    await answer_card(callback,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📜 View Orders", callback_data="orders_menu", style="success"),
                 InlineKeyboardButton(text="🛍 Buy More", callback_data="products_menu", style="primary")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
            ]
        )
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║              CONFIRM PURCHASE                                ║
# ╚══════════════════════════════════════════════════════════════╝

async def _purchase_notifications(callback, telegram_id, product_id, result):
    # Order notification channel
    if ORDER_NOTIFICATION_CHANNEL_ID:
        try:
            uid = str(telegram_id)
            username = callback.from_user.username
            if username:
                masked_user = f"@{username[:2]}***{username[-1:]}" if len(username) > 3 else f"@{username}"
            else:
                masked_user = f"{uid[:4]}***{uid[-3:]}" if len(uid) >= 7 else uid
            delivery_status = (
                "⚡ Delivered instantly" if result.get("status") == "completed"
                else "⏳ Awaiting restock" if result.get("status") == "preorder"
                else "⏳ Delivery processing"
            )
            group_msg = (
                "🛍 <b>PRODUCT ORDER</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                f"👤 <b>User:</b> <code>{_esc(masked_user)}</code>\n"
                f"📦 <b>Product:</b> {_esc(result['name'])}\n"
                f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                f"💰 <b>Total:</b> ${result['total_price']:.2f}\n\n"
                f"{delivery_status}"
            )
            await asyncio.to_thread(enqueue_once, f"public-order:{result['order_id']}", ORDER_NOTIFICATION_CHANNEL_ID, group_msg, callback.bot.id)
        except Exception:
            logger.exception("Failed to send order notification")

    if result.get("low_stock_alert"):
        task = asyncio.create_task(_notify_admins_low_stock(callback.bot, result["low_stock_alert"], result["order_id"]))
        task.add_done_callback(_log_stock_notification_task)
    task = asyncio.create_task(_notify_stock_purchase(callback.bot, telegram_id, result))
    task.add_done_callback(_log_stock_notification_task)
    asyncio.create_task(_refresh_and_scan_product(callback.bot, product_id))

    commission = result.get("referral_commission_paid")
    if commission:
        task = asyncio.create_task(_notify_referral_commission(callback.bot, commission, result["order_id"]))
        task.add_done_callback(_log_stock_notification_task)


@router.callback_query(F.data.startswith("confirm_buy_"))
async def confirm_buy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    telegram_id = callback.from_user.id
    product_id = int(callback.data.split("_")[2])

    lock = await _get_purchase_lock(telegram_id)
    if lock.locked():
        await callback.answer("⏳ Your previous order is still being processed…", show_alert=True)
        return

    async with lock:
        data = await state.get_data()
        quantity = data.get(f"qty_{product_id}", 1)
        token = callback.data.split('_', 3)[3] if len(callback.data.split('_', 3)) == 4 else ''
        if len(token) != 24 or any(c not in '0123456789abcdef' for c in token):
            await show(callback, 'This confirmation expired. Open the product and confirm again.',
                       reply_markup=_cancel_input_keyboard(product_id))
            return
        checkout_key = action_key(callback.bot.id, telegram_id, product_id, token)
        claim = await asyncio.to_thread(claim_checkout, checkout_key, telegram_id, product_id, quantity,
                                        data.get(f'checkout_token_{product_id}') == token)
        if not claim['new']:
            existing_id = claim.get('order_id') or await asyncio.to_thread(recover_checkout, checkout_key, telegram_id)
            text = (f"Order #{existing_id} is already recorded. No new charge." if existing_id else
                    'This confirmation was already used or is processing. Check My Orders before trying again.'
                    if claim['status'] != 'expired' else 'This confirmation expired. Open the product again.')
            await show(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='My Orders', callback_data=f'order_detail_{existing_id}' if existing_id else 'orders_menu'),
                InlineKeyboardButton(text='Home', callback_data='main_menu')]]))
            return


        product = await asyncio.to_thread(_fetch_product, product_id)
        if not product or not product.is_active:
            await asyncio.to_thread(finish_checkout, checkout_key, {"error": "unavailable"})
            await show(callback, f"❌ <b>NOT FOUND</b>\n\n<b>This product is no longer available.</b>",
                       parse_mode="HTML",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu",
                                                 style="primary")]
                       ]))
            return

        if float(product.price) == 0:
            quantity = 1

        is_reseller = getattr(product, "source", "own") == "reseller"

        try:
            if is_reseller:
                async def update_purchase_progress(percent: int, stage: str) -> None:
                    filled = min(10, max(0, percent // 10))
                    progress_bar = "🟩" * filled + "⬜" * (10 - filled)
                    progress_text = (
                        "🛍 <b>ORDER IN PROGRESS</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n\n"
                        f"📦 <b>{_esc(product.name)}</b>\n"
                        f"🔢 Quantity: <b>{quantity}</b>\n\n"
                        f"{progress_bar} <b>{percent}%</b>\n"
                        f"⏳ {_esc(stage)}\n\n"
                        "🔐 Delivery details will appear here when the order is complete."
                    )
                    await show(callback, progress_text, parse_mode="HTML")

                result = await _do_reseller_purchase(
                    telegram_id,
                    product_id,
                    quantity,
                    progress_callback=update_purchase_progress, checkout_key=checkout_key,
                )
            else:
                result = await asyncio.to_thread(_do_purchase, telegram_id, product_id, quantity, None, checkout_key)
        except asyncio.CancelledError:
            logger.warning(
                "Order request interrupted for user=%s product=%s; result may need review",
                telegram_id,
                product_id,
            )
            try:
                await show(
                    callback,
                    (
                        "⚠️ <b>ORDER STATUS NEEDS A CHECK</b>\n\n"
                        "The order request was interrupted before the bot could show its result. "
                        "Open <b>My Orders</b> before trying again so you can confirm whether it completed."
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📜 My Orders", callback_data="orders_menu", style="primary")],
                        [InlineKeyboardButton(text="🆘 Support", callback_data="support_menu", style="danger")],
                    ]),
                )
            except Exception:
                logger.exception("Could not replace interrupted order progress card")
            raise
        except SQLAlchemyError:
            logger.exception("Database error during purchase")
            await show(callback, f"❌ <b>Database Error</b>\n\nOpen My Orders before retrying. Contact support if the result is unclear.",
                       parse_mode="HTML",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="🔄 Try Again", callback_data=f"product_{product_id}",
                                                 style="primary"),
                            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
                       ]))
            return
        except Exception:
            logger.exception("Unexpected error during purchase")
            await show(callback, f"❌ <b>Unexpected Error</b>\n\nOpen My Orders before retrying, or contact support.",
                       parse_mode="HTML",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                           [InlineKeyboardButton(text="🆘 Support", callback_data="support_menu", style="danger"),
                            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
                       ]))
            return

        # Notifications are independent of the screen the buyer opens next.
        if result.get('order_id'):
            task = asyncio.create_task(_purchase_notifications(callback, telegram_id, product_id, result))
            task.add_done_callback(_log_stock_notification_task)
        await asyncio.to_thread(finish_checkout, checkout_key, result)
        if result.get('duplicate_order_id'):
            await show(callback, f"Order #{result['duplicate_order_id']} is already recorded. Open My Orders.")
            return
        await state.update_data(**{f"qty_{product_id}": 1})
        await state.clear()

        if "error" in result:
            error_msg = result["error"]
            is_insufficient = error_msg == "insufficient_balance"

            if is_insufficient:
                total_price = result.get("total_price", 0)
                balance = result.get("balance", 0)
                text = (
                    "💳 <b>INSUFFICIENT BALANCE</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "Your balance isn’t enough for this order.\n\n"
                    f"🛒 <b>Order total:</b> ${total_price:.2f}\n"
                    f"💰 <b>Available balance:</b> ${balance:.2f}\n\n"
                    "Add funds to your wallet or choose another product."
                )
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🏦 💰 Deposit Funds Now",
                                callback_data="deposit_start",
                                style="success"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🛍 Browse Other Products",
                                callback_data="products_menu",
                                style="primary"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 Back to Main Menu",
                                callback_data="main_menu",
                                style="primary"
                            )
                        ]
                    ]
                )
            else:
                text = (
                    "❌ <b>PURCHASE COULDN’T BE COMPLETED</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    f"⚠️ {_esc(error_msg)}\n\n"
                    + ("Check My Orders and contact support before retrying. The payment status needs review."
                     if result.get("needs_review") else "Your wallet has not been charged. Contact support if you need help.")
                )
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="💳 Deposit Funds",
                                callback_data="deposit_start",
                                style="success"
                            ),
                            InlineKeyboardButton(
                                text="🛍 Browse Products",
                                callback_data="products_menu",
                                style="primary"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 Main Menu",
                                callback_data="main_menu",
                                style="primary"
                            )
                        ]
                    ]
                )

            await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)
            return

        is_free = float(result.get("total_price", 0)) == 0
        delivery_instruction = result.get("delivery_instruction")

        def _build_success_keyboard(
            product_id: int,
            order_id: int,
            has_instruction: bool,
        ) -> InlineKeyboardMarkup:
            buttons = []
            if has_instruction:
                buttons.append([
                    InlineKeyboardButton(
                        text="📋 📖 Delivery Instructions",
                        callback_data=f"delivery_instruction_{product_id}",
                        style="primary"
                    )
                ])
            buttons.append([
                InlineKeyboardButton(
                    text="⭐ Rate This Purchase",
                    callback_data=f"order_rate_{order_id}",
                    style="success",
                )
            ])
            buttons.append([
                InlineKeyboardButton(text="📜 View Orders", callback_data="orders_menu", style="success"),
                InlineKeyboardButton(text="🛍 Buy More", callback_data="products_menu", style="primary")
            ])
            buttons.append([
                InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")
            ])
            return InlineKeyboardMarkup(inline_keyboard=buttons)

        if result["status"] == "completed":
            joined_accounts = "\n".join(
                f"{i + 1}. <code>{_esc(str(acc))}</code>"
                for i, acc in enumerate(result["delivered_accounts"])
            )
            has_instr = bool(delivery_instruction)
            charge_text = "🎁 Free" if is_free else f"${result['total_price']:.2f}"
            text = (
                "🛍 <b>PRODUCT PURCHASED!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                f"📦 <b>Product:</b> {_esc(str(result['name']))}\n"
                f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                f"💰 <b>Total:</b> {charge_text}\n\n"
                "⚡ <i>Delivered instantly.</i>\n\n"
                "🔑 <b>Your delivery:</b>\n"
                f"{joined_accounts or '<i>No delivery details were returned.</i>'}\n\n"
                f"💳 <b>Balance:</b> <code>${result['balance']:.2f}</code>"
            )
            if has_instr:
                text += "\n\n📋 Tap <b>Delivery Instructions</b> below for setup help."

            reply_markup = _build_success_keyboard(product_id, result["order_id"], has_instr)

        elif result["status"] == "pending_info":
            charge_text = "🎁 Free" if is_free else f"${result['total_price']:.2f}"
            text = (
                "🧾 <b>ORDER NEEDS YOUR DETAILS</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                f"📦 <b>Product:</b> {_esc(str(result['name']))}\n"
                f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                f"💰 <b>Total:</b> {charge_text}\n\n"
                "Open <b>My Orders</b>, select this order, then tap <b>Send information</b> "
                "to provide the requested details. Delivery is held until you submit them.\n\n"
                f"💳 <b>Balance:</b> <code>${result['balance']:.2f}</code>"
            )
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Open My Orders", callback_data=f"order_detail_{result['order_id']}", style="primary")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")],
            ])
        elif result["status"] == "preorder":
            charge_text = "🎁 Free" if is_free else f"${result['total_price']:.2f}"
            text = (
                "📦 <b>PREORDER CONFIRMED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                f"📦 <b>Product:</b> {_esc(str(result['name']))}\n"
                f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                f"💰 <b>Total:</b> {charge_text}\n"
                "⏳ <b>Status:</b> Waiting for restock\n\n"
                "🔔 We’ll send your delivery as soon as stock is available.\n"
                f"💳 <b>Balance:</b> <code>${result['balance']:.2f}</code>"
            )
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📜 Track Order", callback_data="orders_menu", style="primary"),
                 InlineKeyboardButton(text="🛍 Browse Products", callback_data="products_menu", style="success")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")]
            ])
        else:
            has_instr = bool(delivery_instruction)

            if is_free:
                text = (
                    "⏳ <b>ORDER RECEIVED</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                    f"📦 <b>Product:</b> {_esc(str(result['name']))}\n"
                    f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                    "💰 <b>Total:</b> 🎁 Free\n"
                    "👨‍💼 <b>Delivery:</b> Our team will deliver it\n"
                    "🔔 We’ll message you when it’s ready.\n\n"
                    f"💳 <b>Balance:</b> <code>${result['balance']:.2f}</code>"
                )
            else:
                text = (
                    "⏳ <b>ORDER RECEIVED</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    f"🧾 <b>Order:</b> <code>#{result['order_id']}</code>\n"
                    f"📦 <b>Product:</b> {_esc(str(result['name']))}\n"
                    f"🔢 <b>Quantity:</b> {result['quantity']}\n"
                    f"💰 <b>Total:</b> ${result['total_price']:.2f}\n"
                    "👨‍💼 <b>Delivery:</b> Our team will deliver it\n"
                    "🔔 We’ll message you when it’s ready.\n\n"
                    f"💳 <b>Balance:</b> <code>${result['balance']:.2f}</code>"
                )

            pending_buttons = [
                [InlineKeyboardButton(text="📜 Track Order", callback_data="orders_menu", style="primary"),
                 InlineKeyboardButton(text="🆘 Support", callback_data="support_menu", style="danger")],
            ]
            if has_instr:
                pending_buttons.insert(0, [
                    InlineKeyboardButton(text="📋 📖 Delivery Instructions",
                                          callback_data=f"delivery_instruction_{product_id}", style="primary")
                ])
            pending_buttons.append(
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")])
            reply_markup = InlineKeyboardMarkup(inline_keyboard=pending_buttons)

        if result.get("status") == "pending_manual" and result.get("delivered_accounts"):
            accounts_text = "\n".join(f"<code>{_esc(str(account))}</code>" for account in result["delivered_accounts"])
            text = (f"📦 <b>Order #{result['order_id']} received</b>\n\n"
                    f"✅ Delivered now: {len(result['delivered_accounts'])}\n"
                    f"⏳ Team delivery pending: {result['manual_pending_quantity']}\n\n"
                    f"{accounts_text}\n\nTrack the remaining delivery in My Orders.")
        await show(callback, text, parse_mode="HTML", reply_markup=reply_markup)



# ╔══════════════════════════════════════════════════════════════╗
# ║          RESELLER PRODUCT IMPORT HANDLERS                    ║
# ╚══════════════════════════════════════════════════════════════╝

async def _fetch_and_show_reseller_products(callback: CallbackQuery, reseller_id: str):
    user_id = callback.from_user.id
    lock_key = (user_id, str(reseller_id))

    if lock_key in _reseller_import_locks:
        await callback.answer("⏳ Products are already being fetched from this provider...", show_alert=True)
        return

    _reseller_import_locks.add(lock_key)
    logger.info("IMPORT START user=%s provider=%s", user_id, reseller_id)

    loading_message = await answer_card(callback, "🔄 Fetching products...")

    try:
        db = SessionLocal()
        try:
            from handlers.admin_products import _get_provider_by_id
            provider_config = _get_provider_by_id(db, str(reseller_id))
        except Exception:
            provider_config = None
        finally:
            db.close()

        if not provider_config:
            provider_config = _get_reseller_credentials(reseller_id)

        api_key = provider_config.get("api_key")
        base_url = provider_config.get("base_url")

        if not api_key or not base_url:
            await loading_message.edit_text("❌ Provider credentials not configured properly.")
            schedule_delete_card(loading_message, owner_id=user_id, delay_seconds=900)
            return

        manager = ResellerManager(
            api_key=api_key,
            base_url=base_url,
            provider_config=provider_config
        )

        logger.info("API REQUEST user=%s provider=%s", user_id, reseller_id)
        reseller_data = await _call_reseller_get_products(manager)
        logger.info("API RESPONSE user=%s provider=%s", user_id, reseller_id)

        services = []
        if isinstance(reseller_data, list):
            services = reseller_data
        elif isinstance(reseller_data, dict):
            services = (
                reseller_data.get("services", [])
                or reseller_data.get("products", [])
                or reseller_data.get("data", [])
            )

        services = [
            service for service in services
            if isinstance(service, dict)
            and service.get("is_available") is not False
            and _provider_stock_value(service.get("stock")) > 0
        ]

        if not services:
            await loading_message.edit_text("📭 No in-stock products found from this provider.")
            schedule_delete_card(loading_message, owner_id=user_id, delay_seconds=900)
            return

        text = (
            f"🔗 <b>RESELLER PRODUCTS</b>\n\n"
            f"<b>Provider:</b> {provider_config.get('name', 'Reseller')}\n"
            f"<b>Available Items:</b> {len(services)}\n\n"
            f"{_divider('─', 28)}\n\n"
            f"<i>Select a product below to import or view:</i>"
        )

        keyboard = []
        for s in services[:15]:
            if isinstance(s, dict):
                sid = s.get("service_id") or s.get("id") or "0"
                sname = s.get("name") or s.get("title") or "Product"
                sprice = s.get("rate") or s.get("price") or "0.00"
                keyboard.append([
                    InlineKeyboardButton(
                        text=f"📦 {sname} — ${float(sprice):.2f}",
                        callback_data=f"import_prov_{reseller_id}_{sid}",
                        style="primary"
                    )
                ])

        keyboard.append([
            InlineKeyboardButton(text="🔙 Back to Providers", callback_data="admin_reseller_providers", style="primary"),
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu", style="primary")
        ])

        await loading_message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

    except Exception as e:
        logger.exception("Error fetching reseller products for provider %s", reseller_id)
        try:
            await loading_message.edit_text(f"❌ Error fetching products: {str(e)}")
            schedule_delete_card(loading_message, owner_id=user_id, delay_seconds=900)
        except Exception:
            pass
    finally:
        _reseller_import_locks.discard(lock_key)
        logger.info("IMPORT END user=%s provider=%s", user_id, reseller_id)


@router.callback_query(F.data.startswith("reseller:") | F.data.startswith("provider:"))
async def reseller_selected(callback: CallbackQuery):
    await callback.answer()
    if callback.from_user.id not in ADMIN_IDS:
        return
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return
    reseller_id = parts[1]
    await _fetch_and_show_reseller_products(callback, reseller_id)
