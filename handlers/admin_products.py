
from utils.ui import answer_card, schedule_delete_card
from utils.uploads import text_from_message
import json
import logging
from decimal import Decimal
from html import escape as _esc
import asyncio

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import func

from config import (
    ADMIN_IDS,
    RESELLER_BASE_URL,
    RESELLER_API_KEY,
)
from database import SessionLocal

from models.product import Product
from models.reseller import Reseller

try:
    from models.provider import Provider
except ImportError:
    Provider = None

from states.product_states import AddProduct

from services.reseller_manager import (
    ResellerManager,
    ResellerAPIError,
)

try:
    from services.reseller_config import get_all_resellers, get_reseller
except ImportError:
    get_all_resellers = None
    get_reseller = None

# Notification function import
from handlers.products import notify_new_product, invalidate_products_cache

logger = logging.getLogger(__name__)

router = Router()

print("✅ admin_products imported")

# Per-user per-provider asynchronous locks for preventing concurrent duplicate fetches
_provider_fetch_locks: dict[tuple[int, str], asyncio.Lock] = {}

_ADD_CATEGORY_PAGE_SIZE = 18


def _load_product_category_options() -> list[dict[str, str | int]]:
    """Return unique stored categories and their product counts for admin pickers."""
    db = SessionLocal()
    try:
        rows = (
            db.query(Product.category, func.count(Product.id))
            .filter(Product.category.isnot(None), Product.category != "")
            .group_by(Product.category)
            .order_by(Product.category.asc())
            .all()
        )
    finally:
        db.close()

    merged: dict[str, dict[str, str | int]] = {}
    for raw_category, count in rows:
        name = (raw_category or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in merged:
            merged[key]["count"] = int(merged[key]["count"]) + int(count)
        else:
            merged[key] = {"name": name, "count": int(count)}
    return sorted(merged.values(), key=lambda item: str(item["name"]).casefold())


def _add_product_category_keyboard(options: list[dict[str, str | int]], page: int = 0) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(options) + _ADD_CATEGORY_PAGE_SIZE - 1) // _ADD_CATEGORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _ADD_CATEGORY_PAGE_SIZE
    page_options = options[start:start + _ADD_CATEGORY_PAGE_SIZE]

    rows = []
    row = []
    for index, option in enumerate(page_options, start=start):
        name = str(option["name"])
        label = name if len(name) <= 31 else name[:28].rstrip() + "…"
        row.append(InlineKeyboardButton(
            text=f"🏷 {label} ({option['count']})",
            callback_data=f"add_product_category_pick_{index}",
            style="primary",
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"add_product_category_page_{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="add_product_category_page_current"))
        if page + 1 < total_pages:
            nav.append(InlineKeyboardButton(text="Next ➡", callback_data=f"add_product_category_page_{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅ Back to product type", callback_data="create_product")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_add_product_category_picker(target, state: FSMContext, page: int = 0):
    data = await state.get_data()
    options = data.get("product_category_options", [])
    details = "Choose the catalog this product should be added to."
    if not options:
        details = "No existing catalogs were found. Go back and choose New Catalog or Single Product."
    return await answer_card(
        target,
        f"{_divider('─')}\n\n"
        "🏷 <b>Step 1/10: Existing Catalog</b>\n\n"
        f"{details}",
        parse_mode="HTML",
        reply_markup=_add_product_category_keyboard(options, page),
        state=state,
    )


async def _continue_add_product_after_category(target, state: FSMContext, category: str | None):
    await state.update_data(
        source="own",
        category=category,
        category_input=False,
        reseller_service_id=None,
        reseller_cost=None,
        reseller_name=None,
    )
    await state.set_state(AddProduct.name)
    return await answer_card(
        target,
        f"✅ <b>Catalog:</b> {_esc(category or 'Single product (no catalog)')}\n\n"
        f"{_divider('─')}\n\n"
        "✏️ <b>Step 2/10: Product Name</b>\n\n"
        "Send the product name.\n\n"
        "<i>Example: Gemini Advanced 1 Month</i>",
        parse_mode="HTML",
        state=state,
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║                  UTILITY FUNCTIONS                           ║
# ╚══════════════════════════════════════════════════════════════╝

def _parse_bulk_pricing(raw_text: str) -> dict | None:
    """
    Parse bulk pricing input.
    Format per line: min_qty-max_qty=price
    Example:
    1-10=5.00
    11-50=4.00
    51+=3.00

    Or send "skip" / "none" to skip.
    """
    raw_text = raw_text.strip()

    if raw_text.lower() in ("skip", "none", "no", "n", ""):
        return None

    tiers = {}

    # Try JSON format first
    try:
        tiers = json.loads(raw_text)
        if isinstance(tiers, list):
            result = {}
            for t in tiers:
                key = str(t.get("min", 1))
                result[key] = {
                    "min": int(t.get("min", 1)),
                    "max": int(t["max"]) if t.get("max") else None,
                    "price": float(t.get("price", 0))
                }
            return result if result else None
        elif isinstance(tiers, dict):
            # Validate structure
            for k, v in tiers.items():
                if not isinstance(v, dict):
                    return None
                if "min" not in v or "price" not in v:
                    return None
            return tiers
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Try line-by-line format: 1-10=5.00
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        if "=" not in line:
            return None

        range_part, price_part = line.split("=", 1)
        range_part = range_part.strip()
        price_part = price_part.strip()

        try:
            price = float(price_part)
        except ValueError:
            return None

        if price < 0:
            return None

        if "+" in range_part:
            # 51+ format
            min_qty = int(range_part.replace("+", "").strip())
            key = str(min_qty)
            tiers[key] = {"min": min_qty, "max": None, "price": price}
        elif "-" in range_part:
            # 1-10 format
            parts = range_part.split("-")
            if len(parts) != 2:
                return None
            min_qty = int(parts[0].strip())
            max_qty = int(parts[1].strip())
            if min_qty >= max_qty:
                return None
            key = str(min_qty)
            tiers[key] = {"min": min_qty, "max": max_qty, "price": price}
        else:
            return None

    return tiers if tiers else None


def _format_bulk_pricing_display(bulk_pricing: str | None) -> str:
    """Format bulk pricing for display in product panel."""
    if not bulk_pricing:
        return ""

    try:
        tiers = json.loads(bulk_pricing)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not tiers:
        return ""

    lines = ["\n📦 <b>Bulk Pricing:</b>"]

    sorted_tiers = sorted(tiers.values(), key=lambda x: x.get("min", 0))

    for tier in sorted_tiers:
        min_qty = tier.get("min", 1)
        max_qty = tier.get("max")
        price = tier.get("price", 0)

        if max_qty:
            lines.append(f"  🏷 {min_qty}-{max_qty} units → <b>${price:.2f}</b>/each")
        else:
            lines.append(f"  🏷 {min_qty}+ units → <b>${price:.2f}</b>/each")

    return "\n".join(lines)


def _format_bulk_pricing_plain(bulk_pricing: str | None) -> str:
    """Format bulk pricing as plain text lines for editing."""
    if not bulk_pricing:
        return ""

    try:
        tiers = json.loads(bulk_pricing)
    except (json.JSONDecodeError, TypeError):
        return ""

    lines = []
    sorted_tiers = sorted(tiers.values(), key=lambda x: x.get("min", 0))

    for tier in sorted_tiers:
        min_qty = tier.get("min", 1)
        max_qty = tier.get("max")
        price = tier.get("price", 0)

        if max_qty:
            lines.append(f"{min_qty}-{max_qty}={price:.2f}")
        else:
            lines.append(f"{min_qty}+={price:.2f}")

    return "\n".join(lines)


def _divider(char: str = "━", length: int = 30) -> str:
    return char * length


def _get_all_active_providers(db) -> list[dict]:
    """
    Fetch all active providers dynamically from database models (Provider, Reseller)
    or reseller_config services, falling back to config.py if necessary.
    Completely dynamic with zero hardcoded provider names.
    """
    providers = []
    seen_ids = set()

    # 1. Query Provider model dynamically
    if Provider is not None:
        try:
            db_providers = db.query(Provider).filter(getattr(Provider, "is_active", True) == True).all()
            for p in db_providers:
                pid = str(p.id)
                if pid not in seen_ids:
                    try:
                        provider_config = json.loads(getattr(p, "configuration", None) or "{}")
                    except (TypeError, json.JSONDecodeError):
                        provider_config = {}

                    if not isinstance(provider_config, dict):
                        provider_config = {}

                    provider_config.update({
                        "id": pid,
                        "name": getattr(p, "name", "Provider"),
                        "base_url": (getattr(p, "base_url", "") or "").replace("/docs", "").rstrip("/"),
                        "api_key": getattr(p, "api_key", ""),
                        "api_secret": getattr(p, "api_secret", ""),
                        "type": getattr(p, "api_type", "reseller"),
                        "auth_type": getattr(p, "auth_type", "query"),
                    })
                    providers.append(provider_config)
                    seen_ids.add(pid)
        except Exception:
            pass

    # 2. Query Reseller model dynamically
    if Reseller is not None:
        try:
            db_resellers = db.query(Reseller).filter(getattr(Reseller, "is_active", True) == True).all()
            for r in db_resellers:
                rid = str(r.id)
                if rid not in seen_ids:
                    providers.append({
                        "id": rid,
                        "name": getattr(r, "name", "Reseller"),
                        "base_url": (getattr(r, "base_url", "") or "").replace("/docs", "").rstrip("/"),
                        "api_key": getattr(r, "api_key", ""),
                        "type": "reseller",
                        "auth_type": getattr(r, "auth_type", "query"),
                        "auth_query_param": getattr(r, "auth_query_param", "key"),
                    })
                    seen_ids.add(rid)
        except Exception:
            pass

    # 3. Query get_all_resellers() service dynamically if available
    if get_all_resellers:
        try:
            all_res = get_all_resellers()
            if isinstance(all_res, dict):
                for key, res in all_res.items():
                    key_str = str(key)
                    if key_str not in seen_ids:
                        b_url = getattr(res, "base_url", None) or (res.get("base_url") if isinstance(res, dict) else "")
                        a_key = getattr(res, "api_key", None) or (res.get("api_key") if isinstance(res, dict) else "")
                        a_secret = getattr(res, "api_secret", None) or (res.get("api_secret") if isinstance(res, dict) else "")
                        r_name = getattr(res, "name", None) or (res.get("name") if isinstance(res, dict) else key_str)
                        if b_url and a_key:
                            providers.append({
                                "id": key_str,
                                "name": r_name,
                                "base_url": (b_url or "").replace("/docs", "").rstrip("/"),
                                "api_key": a_key,
                                "api_secret": a_secret,
                                "type": "reseller",
                                "auth_type": getattr(res, "auth_type", "query") if not isinstance(res, dict) else res.get("auth_type", "query"),
                                "auth_query_param": getattr(res, "auth_query_param", "key") if not isinstance(res, dict) else res.get("auth_query_param", "key"),
                            })
                            seen_ids.add(key_str)
            elif isinstance(all_res, list):
                for res in all_res:
                    rid = str(getattr(res, "id", None) or (res.get("id") if isinstance(res, dict) else "default"))
                    if rid not in seen_ids:
                        b_url = getattr(res, "base_url", None) or (res.get("base_url") if isinstance(res, dict) else "")
                        a_key = getattr(res, "api_key", None) or (res.get("api_key") if isinstance(res, dict) else "")
                        a_secret = getattr(res, "api_secret", None) or (res.get("api_secret") if isinstance(res, dict) else "")
                        r_name = getattr(res, "name", None) or (res.get("name") if isinstance(res, dict) else rid)
                        if b_url and a_key:
                            providers.append({
                                "id": rid,
                                "name": r_name,
                                "base_url": (b_url or "").replace("/docs", "").rstrip("/"),
                                "api_key": a_key,
                                "api_secret": a_secret,
                                "type": "reseller",
                                "auth_type": getattr(res, "auth_type", "query") if not isinstance(res, dict) else res.get("auth_type", "query"),
                                "auth_query_param": getattr(res, "auth_query_param", "key") if not isinstance(res, dict) else res.get("auth_query_param", "key"),
                            })
                            seen_ids.add(rid)
        except Exception:
            pass

    # 4. Fallback to config.py environment defaults if database is empty
    if not providers and RESELLER_BASE_URL and RESELLER_API_KEY:
        clean_url = RESELLER_BASE_URL.replace("/docs", "").rstrip("/")
        if clean_url:
            providers.append({
                "id": "default_reseller",
                "name": "Default Provider",
                "base_url": clean_url,
                "api_key": RESELLER_API_KEY,
                "type": "reseller",
                "auth_type": "header",
                "auth_query_param": "key",
            })

    return providers


def _get_provider_by_id(db, provider_id: str | None = None) -> dict | None:
    """
    Retrieve provider configuration by ID.
    If provider_id is specified, strictly look for that provider ID without silent fallback.
    """
    providers = _get_all_active_providers(db)
    if not providers:
        return None

    if provider_id is not None:
        for p in providers:
            if str(p["id"]) == str(provider_id):
                return p
        return None

    return providers[0]


def _load_active_providers() -> list[dict]:
    """Read provider configuration in a worker thread, not on aiogram's loop."""
    db = SessionLocal()
    try:
        return _get_all_active_providers(db)
    finally:
        db.close()


def _load_provider(provider_id: str) -> dict | None:
    db = SessionLocal()
    try:
        return _get_provider_by_id(db, provider_id)
    finally:
        db.close()


def _get_reseller_credentials(reseller_id: str | None = None) -> dict:
    """
    Legacy wrapper for backwards compatibility.
    Retrieves reseller credentials dynamically using provider lookup.
    """
    db = SessionLocal()
    try:
        prov = _get_provider_by_id(db, reseller_id)
    finally:
        db.close()

    if prov:
        return prov

    clean_base_url = (RESELLER_BASE_URL or "").replace("/docs", "").rstrip("/")
    return {
        "id": "default_reseller",
        "base_url": clean_base_url,
        "api_key": RESELLER_API_KEY,
        "name": "Default Provider",
        "auth_type": "header",
    }


# ╔══════════════════════════════════════════════════════════════╗
# ║           START — CREATE PRODUCT FLOW                        ║
# ╚══════════════════════════════════════════════════════════════╝

async def _show_product_source_selection(target: Message | CallbackQuery, state: FSMContext):
    """Ask how the product should be cataloged before collecting details."""
    await state.clear()
    await state.set_state(AddProduct.source)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏷 Add to Existing Catalog",
                    callback_data="addproduct:existing_catalog",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Create New Catalog",
                    callback_data="addproduct:new_catalog",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Add Single Product (No Catalog)",
                    callback_data="addproduct:single_product",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Import from Reseller",
                    callback_data="addproduct:reseller",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="admin_products",
                )
            ]
        ]
    )

    text = (
        "╔══════════════════════════════╗\n"
        "║  📦 CREATE NEW PRODUCT        ║\n"
        "╚══════════════════════════════╝\n\n"
        "Choose how to organize this product before entering its details.\n\n"
        "🏷 <b>Existing Catalog</b> — add it under a category already in your shop.\n"
        "➕ <b>New Catalog</b> — create a category, then add the product.\n"
        "📦 <b>Single Product</b> — add it without a category.\n"
        "🔗 <b>Import from Reseller</b> — fetch a provider product.\n\n"
        "👇 <b>Choose the first step:</b>"
    )

    if isinstance(target, CallbackQuery):
        await answer_card(target, text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await answer_card(target, text, parse_mode="HTML", reply_markup=keyboard)


@router.message(Command("addproduct"))
async def cmd_add_product(message: Message, state: FSMContext):
    """Handle /addproduct command."""
    if message.from_user.id not in ADMIN_IDS:
        return
    await _show_product_source_selection(message, state)


@router.callback_query(F.data == "create_product")
async def cb_add_product(callback: CallbackQuery, state: FSMContext):
    """Handle create_product callback button."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return
    await _show_product_source_selection(callback, state)


@router.callback_query(F.data == "addproduct:existing_catalog")
async def add_product_existing_catalog(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return
    options = await asyncio.to_thread(_load_product_category_options)
    await state.clear()
    await state.update_data(product_category_options=options, category_input=False)
    await state.set_state(AddProduct.category)
    await callback.answer()
    await _show_add_product_category_picker(callback, state)


@router.callback_query(F.data == "addproduct:new_catalog")
async def add_product_new_catalog(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return
    await state.clear()
    await state.update_data(source="own", category_input=True)
    await state.set_state(AddProduct.category)
    await callback.answer()
    await answer_card(
        callback,
        "➕ <b>Create New Catalog</b>\n\n"
        "✏️ <b>Step 1/10: New Catalog</b>\n\n"
        "Send the new category name (up to 255 characters).\n"
        "Example: <code>Gemini AI Pro</code>",
        parse_mode="HTML",
        state=state,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅ Back", callback_data="create_product")
        ]]),
    )


@router.callback_query(F.data == "addproduct:single_product")
async def add_single_product(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return
    await callback.answer()
    await _continue_add_product_after_category(callback, state, None)


@router.callback_query(F.data.in_({"addproduct:own", "add_product_own"}))
async def add_product_own(callback: CallbackQuery, state: FSMContext):
    """Keep old Own Product buttons compatible with the new first-step menu."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return
    await _show_product_source_selection(callback, state)


# ╔══════════════════════════════════════════════════════════════╗
# ║            RESELLER IMPORT FLOW HANDLERS                     ║
# ╚══════════════════════════════════════════════════════════════╝

@router.callback_query(F.data.in_({"addproduct:reseller", "add_product_reseller"}))
async def add_product_reseller(callback: CallbackQuery, state: FSMContext):
    """Initiate reseller import flow and show available active providers dynamically."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return

    providers = await asyncio.to_thread(_load_active_providers)

    if not providers:
        text = (
            "⚠️ <b>No providers configured.</b>\n\n"
            "Please add a provider first from:\n"
            "<b>Admin Panel → 🏪 Providers</b>"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="create_product"
                    )
                ]
            ]
        )
        await answer_card(callback, text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
        return

    buttons = []
    for prov in providers:
        p_id = prov["id"]
        p_name = prov["name"]
        buttons.append([
            InlineKeyboardButton(
                text=f"🏪 {p_name}",
                callback_data=f"reseller_selected:{p_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="⬅️ Back", callback_data="create_product")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        "╔══════════════════════════════╗\n"
        "║  🏪 SELECT PROVIDER          ║\n"
        "╚══════════════════════════════╝\n\n"
        "Choose a provider to import products from:\n"
    )

    await answer_card(callback, text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("reseller_selected:") | F.data.startswith("reseller:") | F.data.startswith("provider:"))
async def reseller_selected(callback: CallbackQuery, state: FSMContext):
    """Handle explicit reseller/provider selection with diagnostic logging and underscore support."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return

    await callback.answer()

    reseller_id = None
    for prefix in ("reseller_selected:", "provider:", "reseller:"):
        if callback.data.startswith(prefix):
            reseller_id = callback.data.split(prefix, 1)[1]
            break
    if not reseller_id:
        reseller_id = callback.data.split(":", 1)[1] if ":" in callback.data else callback.data

    logger.info(
        "Diagnostic Provider Selection | callback_data=%s | extracted_provider_id=%s",
        callback.data,
        reseller_id
    )

    await _fetch_and_show_reseller_products(callback, state, reseller_id=reseller_id)


async def _fetch_and_show_reseller_products(callback: CallbackQuery, state: FSMContext, reseller_id: str | None = None):
    """Fetch live product catalog from selected Provider API and render selection menu with diagnostics."""
    user_id = callback.from_user.id
    lock_key = (user_id, str(reseller_id))

    if lock_key not in _provider_fetch_locks:
        _provider_fetch_locks[lock_key] = asyncio.Lock()

    lock = _provider_fetch_locks[lock_key]

    if lock.locked():
        await callback.answer("⏳ Fetch already in progress...", show_alert=True)
        return

    async with lock:
        prov = await asyncio.to_thread(_load_provider, reseller_id)

        if not prov:
            logger.warning("Diagnostic Provider Config: NOT FOUND for provider_id=%s", reseller_id)
            try:
                await callback.message.edit_text(
                    "❌ <b>Provider Configuration Error:</b>\n"
                    f"Selected provider '{reseller_id}' was not found or is inactive.\n\n"
                    "Please check <b>Admin → Providers</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                await answer_card(callback,
                    "❌ <b>Provider Configuration Error:</b>\n"
                    f"Selected provider '{reseller_id}' was not found or is inactive.\n\n"
                    "Please check <b>Admin → Providers</b>.",
                    parse_mode="HTML"
                )
            return

        base_url = prov.get("base_url", "")
        api_key = prov.get("api_key", "")
        reseller_name = prov.get("name", "Provider")
        prov_id = prov.get("id", reseller_id)
        auth_type = prov.get("auth_type", "query")

        logger.info(
            "Diagnostic Provider Config Found | provider_id=%s | name=%s | base_url=%s | auth_type=%s",
            prov_id,
            reseller_name,
            base_url,
            auth_type
        )

        if not api_key or not base_url:
            logger.warning("Diagnostic Provider Config Incomplete for provider_id=%s", prov_id)
            try:
                await callback.message.edit_text(
                    f"❌ <b>Provider configuration for {_esc(reseller_name)} is incomplete.</b>\n\n"
                    "Please check provider settings in Admin → Providers.",
                    parse_mode="HTML"
                )
            except Exception:
                await answer_card(callback,
                    f"❌ <b>Provider configuration for {_esc(reseller_name)} is incomplete.</b>\n\n"
                    "Please check provider settings in Admin → Providers.",
                    parse_mode="HTML"
                )
            return

        await state.update_data(
            source="reseller",
            reseller_id=prov_id,
            reseller_name=reseller_name
        )

        logger.info(
            "Starting reseller product import: user_id=%s provider=%s",
            user_id,
            prov_id,
        )

        try:
            async with asyncio.timeout(20):
                manager = ResellerManager(api_key=api_key, base_url=base_url, provider_config=prov)
                products = await manager.get_products()
        except asyncio.TimeoutError:
            logger.error("Provider request timed out for provider_id=%s", prov_id)
            try:
                await callback.message.edit_text(
                    f"❌ <b>Connection Timeout:</b> Could not reach provider {_esc(reseller_name)} within timeout.",
                    parse_mode="HTML"
                )
            except Exception:
                await answer_card(callback,
                    f"❌ <b>Connection Timeout:</b> Could not reach provider {_esc(reseller_name)} within timeout.",
                    parse_mode="HTML"
                )
            return
        except ResellerAPIError as e:
            logger.error("Diagnostic ResellerAPIError for provider=%s: %s", prov_id, str(e))
            err_text = str(e)
            display_msg = f"❌ <b>Provider API Error ({_esc(reseller_name)}):</b>\n<code>{_esc(err_text)}</code>\n\n" \
                          "Please check API key and provider settings."

            try:
                await callback.message.edit_text(display_msg, parse_mode="HTML")
            except Exception:
                await answer_card(callback, display_msg, parse_mode="HTML")
            return
        except Exception as e:
            logger.error("Diagnostic Connection Error for provider=%s: %s", prov_id, str(e))
            try:
                await callback.message.edit_text(
                    f"❌ <b>Connection Error:</b> Could not reach provider {_esc(reseller_name)}.\n<code>{_esc(str(e))}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                await answer_card(callback,
                    f"❌ <b>Connection Error:</b> Could not reach provider {_esc(reseller_name)}.\n<code>{_esc(str(e))}</code>",
                    parse_mode="HTML"
                )
            return

        if not isinstance(products, list) or not products:
            try:
                await callback.message.edit_text(f"📦 No products available from {_esc(reseller_name)}.", parse_mode="HTML")
            except Exception:
                await answer_card(callback, f"📦 No products available from {_esc(reseller_name)}.", parse_mode="HTML")
            return

        logger.info(
            "Completed reseller product import: user_id=%s provider=%s count=%s",
            user_id,
            prov_id,
            len(products),
        )

        buttons = []
        products_cache = {}

        for prod in products:
            try:
                if not isinstance(prod, dict):
                    continue
                
                service_id = str(
                    prod.get("service_id") 
                    or prod.get("productId") 
                    or prod.get("product_id") 
                    or prod.get("id") 
                    or ""
                ).strip()

                if not service_id:
                    continue

                name = str(prod.get("name") or prod.get("title") or "Unknown Product")

                raw_price = prod.get("price")
                if isinstance(raw_price, dict):
                    price = float(raw_price.get("amount", 0.0))
                else:
                    try:
                        price = float(raw_price or 0.0)
                    except (ValueError, TypeError):
                        price = 0.0

                raw_stock = prod.get("stock")
                if raw_stock is not None:
                    try:
                        stock_val = int(raw_stock)
                    except (ValueError, TypeError):
                        stock_val = 999999
                else:
                    raw_av = prod.get("availability")
                    if isinstance(raw_av, dict) and "available" in raw_av:
                        try:
                            stock_val = int(raw_av["available"])
                        except (ValueError, TypeError):
                            stock_val = 999999
                    else:
                        stock_val = 999999

                if stock_val <= 0 or prod.get("is_available") is False:
                    continue

                products_cache[service_id] = prod

                if stock_val >= 999999:
                    stock_disp = "🟢 In Stock"
                elif stock_val > 0:
                    stock_disp = f"🟢 {stock_val}"
                else:
                    stock_disp = "🔴 OOS"

                btn_text = f"{name} | Cost: ${price:.2f} | {stock_disp}"
                buttons.append([
                    InlineKeyboardButton(
                        text=btn_text[:64],
                        callback_data=f"reseller_prod:{service_id}"
                    )
                ])
            except Exception:
                logger.exception("Skipping malformed product entry during rendering: %s", prod)
                continue

        if not products_cache:
            try:
                await callback.message.edit_text("No in-stock products available from this provider.", parse_mode="HTML")
            except Exception:
                await answer_card(callback, "No in-stock products available from this provider.", parse_mode="HTML")
            return

        await state.update_data(reseller_products_cache=products_cache)

        buttons.append([
            InlineKeyboardButton(
                text="⬅ Back",
                callback_data="addproduct:reseller"
            )
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await callback.message.edit_text(
                "╔══════════════════════════════╗\n"
                "║  📦 PROVIDER PRODUCTS        ║\n"
                "╚══════════════════════════════╝\n\n"
                f"<b>Provider:</b> {_esc(reseller_name)}\n\n"
                "Select a product from the list below to import:\n\n"
                "<i>Showing item | provider cost | live stock</i>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception:
            logger.exception("Failed to edit Telegram message with reseller product buttons")
            try:
                await answer_card(callback,
                    "╔══════════════════════════════╗\n"
                    "║  📦 PROVIDER PRODUCTS        ║\n"
                    "╚══════════════════════════════╝\n\n"
                    f"<b>Provider:</b> {_esc(reseller_name)}\n\n"
                    "Select a product from the list below to import:\n\n"
                    "<i>Showing item | provider cost | live stock</i>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass


@router.callback_query(F.data.startswith("reseller_prod:"))
async def reseller_product_selected(callback: CallbackQuery, state: FSMContext):
    """Check provider balance and ask how many units to import."""
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Admin only.", show_alert=True)
        return

    service_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    products_cache = data.get("reseller_products_cache", {})

    selected = products_cache.get(service_id)

    if not selected:
        await callback.answer("❌ Product metadata lost. Please try again.", show_alert=True)
        return

    reseller_product_name = selected.get("name") or selected.get("title") or "Reseller Product"
    raw_price = selected.get("price")
    if isinstance(raw_price, dict):
        reseller_cost = Decimal(str(raw_price.get("amount", "0.00")))
    else:
        try:
            reseller_cost = Decimal(str(raw_price or "0.00"))
        except Exception:
            reseller_cost = Decimal("0.00")

    raw_stock = selected.get("stock")
    if raw_stock is not None:
        try:
            reseller_stock = int(raw_stock)
        except (ValueError, TypeError):
            reseller_stock = 999999
    else:
        raw_av = selected.get("availability")
        if isinstance(raw_av, dict) and "available" in raw_av:
            try:
                reseller_stock = int(raw_av["available"])
            except (ValueError, TypeError):
                reseller_stock = 999999
        else:
            reseller_stock = 999999

    reseller_name = data.get("reseller_name", "Reseller")

    provider = await asyncio.to_thread(_get_reseller_credentials, str(data.get("reseller_id") or ""))
    try:
        manager = ResellerManager(
            api_key=provider.get("api_key"),
            base_url=provider.get("base_url"),
            provider_config=provider,
        )
        async with asyncio.timeout(15):
            provider_balance = await manager.get_balance()
    except Exception as exc:
        logger.warning("Could not check provider wallet for import: %s", exc)
        await callback.answer("Could not verify provider wallet balance.", show_alert=True)
        return

    if reseller_cost <= 0:
        await callback.answer("Provider cost is invalid; cannot calculate affordable quantity.", show_alert=True)
        return

    affordable_quantity = int(provider_balance / reseller_cost)
    max_import_quantity = min(reseller_stock, affordable_quantity) if reseller_stock < 999999 else affordable_quantity
    if max_import_quantity <= 0:
        await answer_card(callback,
            f"❌ Provider wallet balance is <b>${provider_balance:.2f}</b>; "
            f"this product costs <b>${reseller_cost:.2f}</b> per unit, so no units can be imported.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    logger.info(
        "Selected Reseller Product | Provider: %s | Service ID: %s | Name: %s | Cost: %s | Stock: %s",
        reseller_name,
        service_id,
        reseller_product_name,
        reseller_cost,
        reseller_stock,
    )

    await state.update_data(
        source="reseller",
        reseller_service_id=service_id,
        name=reseller_product_name,
        reseller_product_name=reseller_product_name,
        reseller_cost=float(reseller_cost),
        reseller_stock=reseller_stock,
        provider_balance=float(provider_balance),
        max_import_quantity=max_import_quantity,
        reseller_name=reseller_name,
        icon=selected.get("emoji", "📦") or "📦",
        category=selected.get("productType", "reseller") or "reseller",
        description=selected.get("description", f"Imported from reseller: {reseller_product_name}"),
        delivery_type="automatic",
    )

    await state.set_state(AddProduct.reseller_quantity)

    stock_display = f"{reseller_stock}" if reseller_stock < 999999 else "🟢 In Stock"

    await answer_card(callback,
        f"🔗 <b>Selected Reseller Product:</b>\n"
        f"<b>{_esc(reseller_product_name)}</b>\n\n"
        f"💰 <b>Provider Cost:</b> ${reseller_cost:.2f}\n"
        f"📦 <b>Provider Stock:</b> {stock_display}\n"
        f"💳 <b>Provider Wallet:</b> ${provider_balance:.2f}\n"
        f"✅ <b>Maximum affordable quantity:</b> {max_import_quantity}\n\n"
        f"🏪 <b>Provider:</b> {_esc(reseller_name)}\n"
        f"🆔 <b>Service ID:</b> <code>{_esc(service_id)}</code>\n\n"
        f"{_divider('─')}\n\n"
        f"📦 <b>Enter the exact quantity to add (1–{max_import_quantity}):</b>",
        parse_mode="HTML"
    )

    await callback.answer()


@router.message(AddProduct.reseller_quantity)
async def reseller_import_quantity(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or not message.text:
        return
    try:
        quantity = int(message.text.strip())
    except ValueError:
        await answer_card(message, "Send a whole-number quantity.")
        return
    data = await state.get_data()
    maximum = int(data.get("max_import_quantity", 0))
    if quantity < 1 or quantity > maximum:
        await answer_card(message, f"Quantity must be between 1 and {maximum}.")
        return
    await state.update_data(reseller_import_quantity=quantity)
    await state.set_state(AddProduct.price)
    await answer_card(message,
        f"✅ Import quantity set to <b>{quantity}</b>.\n"
        f"💰 <b>Enter your selling price (USD):</b>\n\n"
        f"<i>This is the price your customers will pay in your store.</i>\n"
        f"<i>Example: 0.99 or 1.50</i>",
        parse_mode="HTML"
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║                 OWN PRODUCT STEPS (1-10)                     ║
# ╚══════════════════════════════════════════════════════════════╝

@router.message(AddProduct.name)
async def product_name(message: Message, state: FSMContext):
    """Step 2/10: Ask for the product name."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    name = message.text.strip()

    if len(name) < 2:
        await answer_card(message, "❌ <b>Name too short!</b>\nPlease send at least 2 characters.", parse_mode="HTML")
        return

    await state.update_data(name=name)
    await state.set_state(AddProduct.icon)

    await answer_card(message,
        f"✅ <b>Name:</b> {_esc(name)}\n\n"
        f"{_divider('─')}\n\n"
        f"✏️ <b>Step 3/10: Icon</b>\n\n"
        f"Send an emoji for this product.\n\n"
        f"<i>Example: 🎬 or 📧 or 🔑 or 🤖</i>",
        parse_mode="HTML"
    )


@router.message(AddProduct.icon)
async def product_icon(message: Message, state: FSMContext):
    """Step 3/10: Ask for the product price after catalog mode was chosen."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    icon = message.text.strip()

    if len(icon) > 15:
        await answer_card(message, "❌ <b>Icon too long!</b>\nUse 1-15 characters. An emoji is best: 🎬", parse_mode="HTML")
        return

    await state.update_data(icon=icon)
    await state.set_state(AddProduct.price)
    data = await state.get_data()
    category = data.get("category")
    catalog_label = category if category else "Single product (no catalog)"
    await answer_card(
        message,
        f"✅ <b>Icon:</b> {_esc(icon)}\n"
        f"🏷 <b>Catalog:</b> {_esc(catalog_label)}\n\n"
        f"{_divider('─')}\n\n"
        "✏️ <b>Step 4/10: Price</b>\n\n"
        "Send the base price per unit (USD).\n\n"
        "<i>Example: 9.99</i>",
        parse_mode="HTML",
        state=state,
    )


@router.callback_query(F.data.startswith("add_product_category_page_"))
async def add_product_category_page(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    if callback.data.endswith("current"):
        await callback.answer()
        return
    try:
        page = int(callback.data.removeprefix("add_product_category_page_"))
    except ValueError:
        await callback.answer("Invalid category page.", show_alert=True)
        return
    if await state.get_state() != AddProduct.category.state:
        await callback.answer("This product setup has expired. Start again.", show_alert=True)
        return
    await _show_add_product_category_picker(callback, state, page)
    await callback.answer()


@router.callback_query(F.data.startswith("add_product_category_pick_"))
async def add_product_category_pick(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Access denied.", show_alert=True)
        return
    if await state.get_state() != AddProduct.category.state:
        await callback.answer("This product setup has expired. Start again.", show_alert=True)
        return
    try:
        index = int(callback.data.removeprefix("add_product_category_pick_"))
    except ValueError:
        await callback.answer("Invalid category.", show_alert=True)
        return
    data = await state.get_data()
    options = data.get("product_category_options", [])
    if index < 0 or index >= len(options):
        await callback.answer("That category is no longer available.", show_alert=True)
        return
    category = str(options[index]["name"])
    await callback.answer()
    await _continue_add_product_after_category(callback, state, category)


@router.message(AddProduct.category)
async def product_category(message: Message, state: FSMContext):
    """Save the new catalog name before continuing to product details."""
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    if not data.get("category_input"):
        await _show_add_product_category_picker(message, state)
        return
    if not message.text:
        return

    category = message.text.strip()
    if len(category) > 255:
        await answer_card(message, "❌ <b>Category name is too long.</b> Use 255 characters or fewer.", parse_mode="HTML")
        return
    await _continue_add_product_after_category(message, state, category)


@router.message(AddProduct.price)
async def product_price(message: Message, state: FSMContext):
    """Step 4/10: Ask for description (or save/update directly if Reseller product)."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    try:
        price_val = Decimal(message.text.strip())
        price = float(price_val)
    except Exception:
        await answer_card(message, "❌ <b>Invalid price!</b>\nPlease send a positive number like 0.99 or 9.99",
                             parse_mode="HTML")
        return

    data = await state.get_data()

    if price <= 0:
        if price == 0 and not data.get("_zero_confirmed"):
            await state.update_data(_zero_confirmed=True)
            await answer_card(message,
                "⚠️ <b>Price is $0.00 — FREE product!</b>\n\n"
                "Send <b>0</b> again to confirm.",
                parse_mode="HTML"
            )
            return
        elif price < 0:
            await answer_card(message, "❌ <b>Price can't be negative!</b>", parse_mode="HTML")
            return

    await state.update_data(price=price)

    if data.get("source") == "reseller":
        await _ask_product_aftercare(message, state)
        return

    # Own product sequence continues
    await state.set_state(AddProduct.description)

    await answer_card(message,
        f"✅ <b>Price:</b> ${price:.2f}\n\n"
        f"{_divider('─')}\n\n"
        f"✏️ <b>Step 5/10: Description</b>\n\n"
        f"Send a description for this product.\n\n"
        f"<i>Example: Premium Gemini Advanced account\n"
        f"with 1-month validity. Includes all features.</i>\n\n"
        f"💡 <i>Send 'skip' to leave empty</i>",
        parse_mode="HTML"
    )




async def _save_reseller_product(message, state, data, price):
    reseller_name = data.get("reseller_name", "Reseller")
    reseller_service_id = data.get("reseller_service_id")
    reseller_cost = data.get("reseller_cost")
    reseller_stock = data.get("reseller_import_quantity", 0)

    db = SessionLocal()
    try:
        reseller_id_val = data.get("reseller_id")
        parsed_provider_id = None

        if reseller_id_val is not None:
            try:
                parsed_provider_id = int(reseller_id_val)
            except (ValueError, TypeError):
                if Provider is not None:
                    db_prov = db.query(Provider).filter(
                        (Provider.name == str(reseller_id_val)) |
                        (getattr(Provider, "id", None) == reseller_id_val)
                    ).first()
                    if db_prov:
                        parsed_provider_id = db_prov.id

        # Locate existing database record if product was previously imported
        existing_product = None
        if parsed_provider_id is not None and reseller_service_id:
            existing_product = db.query(Product).filter(
                Product.provider_id == parsed_provider_id,
                Product.reseller_service_id == str(reseller_service_id)
            ).first()

        if not existing_product and reseller_service_id:
            existing_product = db.query(Product).filter(
                Product.reseller_service_id == str(reseller_service_id)
            ).first()

        if existing_product:
            existing_product.name = data["name"]
            existing_product.price = price
            existing_product.reseller_cost = reseller_cost
            existing_product.reseller_name = reseller_name
            existing_product.stock = reseller_stock
            existing_product.is_active = True
            product = existing_product
            action_str = "Updated"
        else:
            product = Product(
                name=data["name"],
                source="reseller",
                provider_id=parsed_provider_id,
                reseller_service_id=str(reseller_service_id) if reseller_service_id is not None else None,
                reseller_cost=reseller_cost,
                reseller_name=reseller_name,
                icon=data.get("icon", "📦"),
                category=data.get("category", "reseller"),
                description=data.get("description", f"Imported from reseller: {data['name']}"),
                price=price,
                stock=reseller_stock,
                file_content=None,
                is_active=True,
                delivery_type=data.get("delivery_type", "automatic"),
                delivery_instruction=data.get("delivery_instruction", None),
                required_info=None,
                preorder=False,
                bulk_pricing=None,
                low_stock_threshold=3,
            )
            db.add(product)
            action_str = "Created"

        for field in ("aftercare_policy", "warranty_days", "subscription_days"):
            setattr(product, field, data.get(field, "skip" if field == "aftercare_policy" else 0))
        db.commit()
        invalidate_products_cache()
        db.refresh(product)
        pid = product.id

        logger.info(
            "Reseller Product Saved (%s) | Product ID: %d | Provider Service ID: %s | Name: %s | Price: %s | Cost: %s | Stock: %d",
            action_str,
            pid,
            reseller_service_id,
            product.name,
            product.price,
            reseller_cost,
            product.stock,
        )
    finally:
        db.close()

    await state.clear()

    stock_display = f"{product.stock}" if product.stock < 999999 else "🟢 In Stock"

    text = (
        f"✅ <b>Reseller Product {action_str}</b>\n\n"
        f"🔗 <b>{_esc(product.name)}</b>\n\n"
        f"💰 <b>Reseller Cost:</b>\n${float(reseller_cost or 0.0):.2f}\n\n"
        f"💵 <b>Selling Price:</b>\n${float(product.price):.2f}\n\n"
        f"📦 <b>Stock:</b>\n{stock_display}\n\n"
        f"🔗 <b>Reseller:</b>\n{_esc(reseller_name)}\n\n"
        f"🆔 <b>Service:</b>\n<code>{_esc(str(reseller_service_id))}</code>\n\n"
        f"🆔 <b>Product ID:</b> #{pid}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Product Manager", callback_data="admin_products")],
            [InlineKeyboardButton(text="➕ Create Another", callback_data="create_product")]
        ]
    )

    await answer_card(message,
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )
    return



@router.message(AddProduct.description)
async def product_description(message: Message, state: FSMContext):
    """Step 6/10: Ask for delivery type."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    desc = message.text.strip()

    if desc.lower() == "skip":
        desc = ""

    await state.update_data(description=desc)
    await _ask_product_aftercare(message, state)


async def _ask_product_aftercare(target, state):
    await state.set_state(AddProduct.aftercare_policy)
    await answer_card(target, 'Choose what buyers can claim after delivery.\n'
                      'Warranty: replacement within your coverage period.\n'
                      'Refund: your usage-based formula, reviewed by an admin.\n'
                      'Skip: no warranty/refund button.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                          [InlineKeyboardButton(text='🛡 Warranty', callback_data='addcare_warranty')],
                          [InlineKeyboardButton(text='💸 Refund', callback_data='addcare_refund')],
                          [InlineKeyboardButton(text='Skip', callback_data='addcare_skip')],
                          [InlineKeyboardButton(text='Cancel', callback_data='admin_products')]]))


@router.callback_query(F.data.startswith('addcare_'))
async def product_aftercare_choice(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Admin only.', show_alert=True)
        return
    mode = callback.data.removeprefix('addcare_')
    if await state.get_state() != AddProduct.aftercare_policy.state or mode not in ('warranty', 'refund', 'skip'):
        await callback.answer('Start product setup again.', show_alert=True)
        return
    await callback.answer()
    await state.update_data(aftercare_policy=mode, warranty_days=0, subscription_days=0)
    if mode == 'skip':
        await _continue_aftercare_product(callback, state)
        return
    await state.set_state(AddProduct.aftercare_days)
    from services.warranty import REFUND_RULES
    prompt = 'Enter replacement warranty days (1–3650).' if mode == 'warranty' else (
        REFUND_RULES+'\n\nEnter subscription length in days (1–3650).')
    await answer_card(callback, prompt, parse_mode='HTML')


@router.message(AddProduct.aftercare_policy)
async def product_aftercare_prompt(message: Message, state: FSMContext):
    if message.from_user.id in ADMIN_IDS:
        from services.input_forms import is_cancel
        if is_cancel(message.text):
            await state.clear()
            await answer_card(message, 'Product setup cancelled.')
            return
        await _ask_product_aftercare(message, state)


@router.message(AddProduct.aftercare_days)
async def product_aftercare_days(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    from services.input_forms import is_cancel
    if is_cancel(message.text):
        await state.clear()
        await answer_card(message, 'Product setup cancelled.')
        return
    raw = (message.text or '').strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 4 or not 1 <= int(raw) <= 3650:
        await answer_card(message, 'Send 1–3650 days, or /cancel.')
        return
    data = await state.get_data()
    field = 'warranty_days' if data['aftercare_policy'] == 'warranty' else 'subscription_days'
    await state.update_data(**{field: int(raw)})
    await _continue_aftercare_product(message, state)


async def _continue_aftercare_product(target, state):
    data = await state.get_data()
    if data.get('source') == 'reseller':
        await _save_reseller_product(target, state, data, data['price'])
        return
    await state.set_state(AddProduct.delivery_type)
    await answer_card(target, '✅ Aftercare saved.\n\nChoose delivery type:\n'
                      'automatic — instant delivery\nmanual — admin team\nhybrid — accounts first, then admin\n\n'
                      'Send: automatic, manual, or hybrid.')


@router.message(AddProduct.delivery_type)
async def product_delivery(message: Message, state: FSMContext):
    """Step 7/10: Ask for delivery instruction."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    dt = message.text.strip().lower()

    if dt not in ("automatic", "manual", "hybrid"):
        await answer_card(message,
            "❌ <b>Invalid!</b>\n\n"
            "Please send one of:\n"
            "• <b>automatic</b>\n"
            "• <b>manual</b>\n"
            "• <b>hybrid</b>",
            parse_mode="HTML"
        )
        return

    await state.update_data(delivery_type=dt)
    if dt != "manual":
        await state.set_state(AddProduct.manual_fallback)
        await answer_card(message, "Allow manual delivery if uploaded accounts run out?\n"
                          "Send yes or no. Hybrid orders deliver ready accounts first.")
        return
    await state.update_data(manual_fallback=True)
    await _ask_delivery_instruction(message, state, dt)


@router.message(AddProduct.manual_fallback)
async def product_manual_fallback(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    raw = (message.text or "").strip().lower()
    if raw not in {"yes", "no"}:
        await answer_card(message, "Send yes or no.")
        return
    await state.update_data(manual_fallback=raw == "yes")
    data = await state.get_data()
    await _ask_delivery_instruction(message, state, data["delivery_type"])


async def _ask_delivery_instruction(message, state, dt):
    await state.set_state(AddProduct.delivery_instruction)

    delivery_labels = {
        "automatic": "🤖 Auto-Delivery",
        "manual": "👨‍💼 Manual Delivery",
        "hybrid": "🔀 Hybrid",
    }

    await answer_card(message,
        f"✅ <b>Delivery:</b> {delivery_labels.get(dt, dt)}\n\n"
        f"{_divider('─')}\n\n"
        f"✏️ <b>Step 7/10: Delivery Instructions</b> <i>(Optional)</i>\n\n"
        f"📋 <b>What are delivery instructions?</b>\n"
        f"These are shown to the buyer AFTER a successful\n"
        f"purchase. They appear as a clickable button in the\n"
        f"purchase confirmation message.\n\n"
        f"{_divider('─')}\n\n"
        f"📝 <b>Examples of instructions:</b>\n"
        f"  • \"Use a VPN when logging into this account\"\n"
        f"  • \"Change password within 24 hours\"\n"
        f"  • \"Account valid for 30 days — do not share\"\n"
        f"  • \"Check spam folder for verification email\"\n"
        f"  • \"Do not change recovery email or phone\"\n\n"
        f"{_divider('─')}\n\n"
        f"📤 <b>Send your instruction now</b>\n"
        f"OR send <b>skip</b> for no instructions\n\n"
        f"<i>This message will be shown as important\n"
        f"information the buyer must read!</i>",
        parse_mode="HTML"
    )


@router.message(AddProduct.delivery_instruction)
async def product_delivery_instruction(message: Message, state: FSMContext):
    """Ask which buyer details are required for this product."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    instruction = message.text.strip()

    if instruction.lower() in ("skip", "none", "no", ""):
        instruction = None

    await state.update_data(delivery_instruction=instruction)
    await state.set_state(AddProduct.required_info)

    await answer_card(message,
        f"✅ <b>Delivery Instruction:</b> {_esc(instruction) if instruction else '(none set)'}\n\n"
        f"{_divider('─')}\n\n"
        f"✏️ <b>Step 8/11: Buyer information</b> <i>(Optional)</i>\n\n"
        f"If fulfillment needs details from the buyer, enter one requirement per line.\n"
        f"After buying, the customer will open My Orders and use <b>Send information</b>.\n\n"
        f"Formats:\n"
        f"<code>email:Email address</code>\n"
        f"<code>phone:Contact number</code>\n"
        f"<code>photo:Upload a screenshot</code>\n"
        f"<code>contact:Share your Telegram contact</code>\n"
        f"<code>text:Other required information</code>\n\n"
        f"Send <b>skip</b> if no buyer information is needed.",
        parse_mode="HTML"
    )


@router.message(AddProduct.required_info)
async def product_required_info(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS or not message.text:
        return
    raw = message.text.strip()
    requirements = []
    if raw.lower() not in {"skip", "none", "no", "n"}:
        allowed = {"email", "phone", "photo", "contact", "text"}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            kind, sep, label = line.partition(":")
            kind = kind.strip().lower()
            if kind not in allowed:
                await answer_card(message,
                    "❌ Use <code>email</code>, <code>phone</code>, <code>photo</code>, "
                    "<code>contact</code>, or <code>text</code>. Example: "
                    "<code>email:Customer email</code>", parse_mode="HTML")
                return
            requirements.append({"type": kind, "label": (label.strip() if sep and label.strip() else kind.title())[:100]})
        if len(requirements) > 8:
            await answer_card(message, "❌ You can add up to 8 required fields.")
            return
    await state.update_data(required_info=requirements or None)
    await state.set_state(AddProduct.preorder)
    await answer_card(message,
        f"✅ Buyer information fields: <b>{len(requirements)}</b>\n\n"
        "✏️ <b>Step 9: Preorder</b>\nAllow orders when stock is empty? Send <b>yes</b> or <b>no</b>.",
        parse_mode="HTML")


@router.message(AddProduct.preorder)
async def product_preorder(message: Message, state: FSMContext):
    """Step 9/10: Ask for bulk pricing (optional)."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    raw = message.text.strip().lower()
    preorder = raw in ("yes", "y", "true", "1", "enable", "on")
    data = await state.get_data()
    if preorder and data.get('delivery_type') != 'manual' and not data.get('manual_fallback'):
        await answer_card(message, 'Preorders require manual fallback. Send no to keep strict automatic delivery.')
        return

    await state.update_data(preorder=preorder)
    await state.set_state(AddProduct.bulk_pricing)

    text = (
        f"✅ <b>Preorder:</b> {'🟢 Yes' if preorder else '🔴 No'}\n\n"
        f"{_divider('═')}\n\n"
        f"✏️ <b>Step 9/10: Bulk Pricing</b> <i>(Optional)</i>\n\n"
        f"{_divider('─')}\n\n"
        f"📦 <b>Want to add tiered/bulk pricing?</b>\n\n"
        f"Buyers automatically get discounts\n"
        f"when they purchase more units!\n\n"
        f"{_divider('─')}\n\n"
        f"📝 <b>How to format (one per line):</b>\n\n"
        f"<code>1-10=5.00</code>\n"
        f"  └ 1-10 units → $5.00 each\n\n"
        f"<code>11-50=4.00</code>\n"
        f"  └ 11-50 units → $4.00 each\n\n"
        f"<code>51+=3.00</code>\n"
        f"  └ 51+ units → $3.00 each\n\n"
        f"{_divider('─')}\n\n"
        f"📤 <b>Send your tiers now</b>\n"
        f"OR send <b>skip</b> for flat pricing only\n\n"
        f"<i>Example message:</i>\n"
        f"<code>1-10=5.00\n11-50=4.00\n51+=3.00</code>"
    )

    await answer_card(message, text, parse_mode="HTML")


@router.message(AddProduct.bulk_pricing)
async def product_bulk_pricing(message: Message, state: FSMContext):
    """Step 10/10: Ask for accounts."""
    if message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    raw = message.text.strip()

    # Check if skipped
    if raw.lower() in ("skip", "none", "no", "n", ""):
        await state.update_data(bulk_pricing=None)
        await state.set_state(AddProduct.accounts)

        await answer_card(message,
            f"✅ <b>Bulk Pricing:</b> Skipped\n"
            f"    └ Using flat pricing: base price applies to all quantities\n\n"
            f"{_divider('═')}\n\n"
            f"✏️ <b>Step 10/10: Accounts</b>\n\n"
            f"Send the accounts for this product.\n\n"
            f"<b>Format:</b> One account per line\n"
            f"<code>email1@gmail.com:password1</code>\n"
            f"<code>email2@gmail.com:password2</code>\n\n"
            f"📊 Stock will be set automatically from\n"
            f"the number of accounts you provide.\n\n"
            f"💡 <i>Send 'skip' if no accounts yet</i>",
            parse_mode="HTML"
        )
        return

    # Parse bulk pricing
    bulk_data = _parse_bulk_pricing(raw)

    if bulk_data is None:
        await answer_card(message,
            "❌ <b>Invalid format!</b>\n\n"
            f"{_divider('─')}\n\n"
            "Please use the correct format:\n\n"
            "<code>1-10=5.00</code>\n"
            "<code>11-50=4.00</code>\n"
            "<code>51+=3.00</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 Rules:\n"
            "• One tier per line\n"
            "• Format: MIN-MAX=PRICE or MIN+=PRICE\n"
            "• Prices must be numbers\n"
            "• Ranges cannot overlap\n\n"
            "OR send <b>skip</b> for flat pricing.",
            parse_mode="HTML"
        )
        return

    # Validate tiers make sense
    sorted_tiers = sorted(bulk_data.values(), key=lambda x: x.get("min", 0))
    for i in range(len(sorted_tiers) - 1):
        current = sorted_tiers[i]
        next_tier = sorted_tiers[i + 1]
        if current.get("max") and current["max"] >= next_tier.get("min", 0):
            await answer_card(message,
                "❌ <b>Overlapping tiers!</b>\n\n"
                f"Tier {current.get('min')}-{current.get('max')} overlaps with "
                f"tier starting at {next_tier.get('min')}.\n\n"
                "Please fix and send again.",
                parse_mode="HTML"
            )
            return

    # Format confirmation
    confirm_lines = ["✅ <b>Bulk Pricing Set:</b>\n"]
    for tier in sorted_tiers:
        min_q = tier.get("min", 1)
        max_q = tier.get("max")
        price = tier.get("price", 0)
        if max_q:
            confirm_lines.append(f"  🏷 {min_q}-{max_q} units → <b>${price:.2f}</b>/each")
        else:
            confirm_lines.append(f"  🏷 {min_q}+ units → <b>${price:.2f}</b>/each")

    bulk_json = json.dumps(bulk_data)
    await state.update_data(bulk_pricing=bulk_json)
    await state.set_state(AddProduct.accounts)

    confirm_lines.append(f"\n{_divider('═')}")
    confirm_lines.append(f"\n✏️ <b>Step 10/10: Accounts</b>\n")
    confirm_lines.append("Send the accounts for this product.\n")
    confirm_lines.append("<b>Format:</b> One account per line\n")
    confirm_lines.append("<code>email1@gmail.com:password1</code>\n")
    confirm_lines.append("<code>email2@gmail.com:password2</code>\n")
    confirm_lines.append("\n💡 <i>Send 'skip' if no accounts yet</i>")

    await answer_card(message, "\n".join(confirm_lines), parse_mode="HTML")


@router.message(AddProduct.accounts)
async def save_product(message: Message, state: FSMContext):
    """Save the own product to database."""
    if message.from_user.id not in ADMIN_IDS:
        return

    try:
        account_text = await text_from_message(message)
    except ValueError as exc:
        await answer_card(message, f"❌ {_esc(str(exc))}")
        return
    if account_text is None:
        await answer_card(message, "Send account lines as text or upload a .txt/.csv file. Send <b>skip</b> for no stock.", parse_mode="HTML")
        return

    data = await state.get_data()
    raw_text = account_text.strip()

    # Handle accounts
    if raw_text.lower() in ("skip", "none", "no", ""):
        accounts = []
        file_content = ""
    else:
        accounts = [x.strip() for x in raw_text.splitlines() if x.strip()]
        file_content = "\n".join(accounts)

    await state.update_data(draft_accounts=accounts)
    await _finish_own_product(message, state)


@router.message(AddProduct.empty_inventory)
async def product_empty_inventory(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    raw = (message.text or "").strip().lower()
    if raw not in {"yes", "no"}:
        await answer_card(message, "Switch to manual delivery? Send yes or no.")
        return
    await state.update_data(empty_inventory_decided=True)
    if raw == "yes":
        await state.update_data(delivery_type="manual", manual_fallback=True)
    await _finish_own_product(message, state)


@router.message(AddProduct.stock_capacity)
async def product_stock_capacity(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    if not raw.isdigit() or not len(data.get("draft_accounts", [])) <= int(raw) <= 1000000:
        await answer_card(message, "Enter an order capacity between your uploaded account count and 1,000,000.")
        return
    await state.update_data(stock_capacity=int(raw))
    await _finish_own_product(message, state)


async def _finish_own_product(message, state):
    data = await state.get_data()
    accounts = data.get("draft_accounts", [])
    file_content = "\n".join(accounts)
    if not accounts and data.get("delivery_type") in {"automatic", "hybrid"} and not data.get("manual_fallback") and not data.get("empty_inventory_decided"):
        await state.set_state(AddProduct.empty_inventory)
        await answer_card(message, "No accounts uploaded. Switch to manual delivery?\n"
                          "Send yes, or no to keep this product blocked until accounts are added.")
        return
    if (data.get("manual_fallback") or data.get("delivery_type") == "manual") and "stock_capacity" not in data:
        await state.set_state(AddProduct.stock_capacity)
        await answer_card(message, f"Uploaded accounts: {len(accounts)}\nEnter order capacity, e.g. 1000.\n"
                          "Accounts deliver first; the team completes the rest.")
        return
    # Save to database
    db = SessionLocal()
    try:
        product = Product(
            name=data["name"],
            source="own",
            reseller_service_id=None,
            reseller_cost=None,
            reseller_name=None,
            icon=data.get("icon", "📦"),
            category=data.get("category", "general"),
            description=data.get("description", ""),
            price=data["price"],
            stock=data.get("stock_capacity", len(accounts)),
            manual_fallback=bool(data.get("manual_fallback")),
            file_content=file_content if file_content else None,
            is_active=True,
            delivery_type=data.get("delivery_type", "automatic"),
            delivery_instruction=data.get("delivery_instruction", None),
            required_info=data.get("required_info"),
            aftercare_policy=data.get("aftercare_policy", "skip"),
            warranty_days=data.get("warranty_days", 0),
            subscription_days=data.get("subscription_days", 0),
            preorder=data.get("preorder", False),
            bulk_pricing=data.get("bulk_pricing", None),
            low_stock_threshold=3,
        )

        db.add(product)
        db.commit()
        invalidate_products_cache()
        db.refresh(product)
        pid = product.id
    finally:
        db.close()

    await state.clear()

    # Stock notification trigger
    try:
        if hasattr(message, "bot"):
            await notify_new_product(message.bot, product)
        elif hasattr(message, "_bot"):
            await notify_new_product(message._bot(), product)
    except Exception:
        logger.exception("Failed to send stock notification for new product %s", pid)

    # Build success message
    text_parts = [
        "╔══════════════════════════════╗",
        "║  ✅ PRODUCT CREATED ✨        ║",
        "╚══════════════════════════════╝",
        "",
        f"🆔 <b>ID:</b> {pid}",
        f"📦 <b>Name:</b> {product.icon} {_esc(product.name)}",
        f"🏷 <b>Category:</b> {_esc(product.category)}",
        f"💰 <b>Base Price:</b> ${float(product.price):.2f}",
        f"📊 <b>Stock:</b> {product.stock}",
        f"🚚 <b>Delivery:</b> {product.delivery_type}",
        f"Manual fallback: {'Yes' if product.manual_fallback else 'No'}",
        f"🧾 <b>Buyer details required:</b> {len(product.required_info or [])}",
        f"📦 <b>Preorder:</b> {'🟢 Yes' if product.preorder else '🔴 No'}",
    ]

    # Delivery instruction section
    text_parts.append(f"\n{_divider('─')}")
    if product.delivery_instruction:
        text_parts.append(f"\n📋 <b>Delivery Instructions:</b>")
        text_parts.append(f"    └ \"{_esc(product.delivery_instruction[:200])}\"")
        if len(product.delivery_instruction) > 200:
            text_parts.append("    ...(truncated)")
    else:
        text_parts.append(f"\n📋 <b>Delivery Instructions:</b> ❌ Not set")

    # Bulk pricing section
    text_parts.append(f"\n{_divider('─')}")

    if product.bulk_pricing:
        text_parts.append(_format_bulk_pricing_display(product.bulk_pricing))
    else:
        text_parts.append("\n📦 <b>Bulk Pricing:</b> ❌ Not set")
        text_parts.append("    └ All quantities at base price")

    text_parts.append(f"\n{_divider('─')}")

    if product.description:
        text_parts.append(f"\n📝 <b>Description:</b> {_esc(product.description[:300])}")
        if len(product.description) > 300:
            text_parts.append("    ...(truncated)")

    if accounts:
        text_parts.append(f"\n🔑 <b>Accounts loaded:</b> {len(accounts)}")

    text_parts.append(f"\n{_divider('═')}")
    text_parts.append("\n✅ <b>Product is now live!</b>")

    text = "\n".join(text_parts)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Product Manager",
                    callback_data="admin_products",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Create Another",
                    callback_data="create_product",
                ),
                InlineKeyboardButton(
                    text="📋 Manage This Product",
                    callback_data=f"manage_{pid}",
                )
            ]
        ]
    )

    created_card = await answer_card(message, text, parse_mode="HTML", reply_markup=keyboard)
    if created_card:
        schedule_delete_card(created_card, owner_id=message.from_user.id, delay_seconds=900)
