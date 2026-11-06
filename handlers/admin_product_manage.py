
from utils.ui import answer_card, schedule_success_notice
from utils.uploads import text_from_message
# handlers/admin_product_manage.py — FULLY WORKING ADMIN PRODUCT MANAGEMENT

import json
import logging
import asyncio
from decimal import Decimal
from html import escape as html_escape

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from config import ADMIN_IDS
from database import SessionLocal
from models.product import Product
from services.fulfillment_policy import manual_allowed
from services.warranty import policy_label
from models.admin_audit_log import AdminAuditLog
from states.product_states import AddAccounts, EditAccounts, EditBulkPricing, EditProduct
from handlers.products import (
    _real_stock,
    _send_stock_channel_message,
    _notify_restock_subscribers,
    invalidate_products_cache,
)

router = Router()
logger = logging.getLogger(__name__)

DELIVERY_TYPES = ["automatic", "manual", "hybrid"]
DELIVERY_LABELS = {
    "automatic": "⚡ Automatic",
    "manual": "🖐 Manual",
    "hybrid": "🔀 Hybrid",
}
DEFAULT_LOW_STOCK_THRESHOLD = 3
_BULK_PAGE_SIZE = 8


def _bulk_keyboard(products, selected_ids: set[int], page: int, total_pages: int):
    rows = []
    for product in products:
        selected = product.id in selected_ids
        status = "🟢" if product.is_active else "🔴"
        api_status = "API✓" if getattr(product, "api_enabled", True) else "API✗"
        rows.append([InlineKeyboardButton(
            text=f"{'☑' if selected else '☐'} #{product.id} {status} {product.name[:22]} · {api_status}",
            callback_data=f"bulk_select_{product.id}_{page}",
        )])
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅ Prev", callback_data=f"bulk_page_{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="bulk_cancel"))
    if page + 1 < total_pages:
        navigation.append(InlineKeyboardButton(text="Next ➡", callback_data=f"bulk_page_{page + 1}"))
    rows.append(navigation)
    rows.extend([
        [InlineKeyboardButton(text="🛍 Disable Shop", callback_data="bulk_confirm_shop_off"),
         InlineKeyboardButton(text="🛍 Enable Shop", callback_data="bulk_confirm_shop_on")],
        [InlineKeyboardButton(text="🔌 Disable API", callback_data="bulk_confirm_api_off"),
         InlineKeyboardButton(text="🔌 Enable API", callback_data="bulk_confirm_api_on")],
        [InlineKeyboardButton(text="🧹 Clear Selection", callback_data="bulk_clear"),
         InlineKeyboardButton(text="⬅ Back", callback_data="admin_products")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_bulk_manager(callback: CallbackQuery, state: FSMContext, page: int = 0):
    db = SessionLocal()
    try:
        total = db.query(Product).count()
        total_pages = (total + _BULK_PAGE_SIZE - 1) // _BULK_PAGE_SIZE
        page = max(0, min(page, max(total_pages - 1, 0)))
        products = (db.query(Product).order_by(Product.id.asc())
                    .offset(page * _BULK_PAGE_SIZE).limit(_BULK_PAGE_SIZE).all())
    finally:
        db.close()
    data = await state.get_data()
    selected_ids = {int(value) for value in data.get("bulk_selected_ids", [])}
    text = (
        "🧰 <b>BULK PRODUCT MANAGER</b>\n"
        "Select products, then choose a shop/API action.\n"
        "Inventory and provider credentials are not changed.\n\n"
        f"Page {page + 1}/{max(total_pages, 1)} · {total} products\n"
        f"Selected: <b>{len(selected_ids)}</b>"
    )
    if not products:
        text += "\n\nNo products found."
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=_bulk_keyboard(products, selected_ids, page, total_pages),
    )


@router.callback_query(F.data == "bulk_products")
async def bulk_products(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.update_data(bulk_selected_ids=[])
    await _show_bulk_manager(callback, state)
    await callback.answer()


@router.callback_query(F.data == "bulk_cancel")
async def bulk_products_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await _show_bulk_manager(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("bulk_page_"))
async def bulk_products_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await _show_bulk_manager(callback, state, int(callback.data.rsplit("_", 1)[1]))
    await callback.answer()


@router.callback_query(F.data.startswith("bulk_select_"))
async def bulk_product_select(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    _, _, product_id, page = callback.data.split("_")
    data = await state.get_data()
    selected = {int(value) for value in data.get("bulk_selected_ids", [])}
    product_id = int(product_id)
    selected.symmetric_difference_update({product_id})
    await state.update_data(bulk_selected_ids=sorted(selected))
    await _show_bulk_manager(callback, state, int(page))
    await callback.answer("Selection updated")


@router.callback_query(F.data == "bulk_clear")
async def bulk_products_clear(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.update_data(bulk_selected_ids=[])
    await _show_bulk_manager(callback, state)
    await callback.answer("Selection cleared")


@router.callback_query(F.data.startswith("bulk_confirm_"))
async def bulk_products_confirm_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    action = callback.data.removeprefix("bulk_confirm_")
    data = await state.get_data()
    selected = data.get("bulk_selected_ids", [])
    labels = {
        "shop_off": "hide selected products from the shop",
        "shop_on": "show selected products in the shop",
        "api_off": "disable API exposure for selected products",
        "api_on": "enable API exposure for selected products",
    }
    if not selected or action not in labels:
        await callback.answer("Select products and choose a valid action.", show_alert=True)
        return
    await state.update_data(bulk_pending_action=action)
    await callback.message.edit_text(
        f"⚠️ <b>Confirm bulk change</b>\n\nThis will {labels[action]} "
        f"for <b>{len(selected)}</b> product(s). Inventory will not be changed.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm", callback_data="bulk_apply")],
            [InlineKeyboardButton(text="Cancel", callback_data="bulk_cancel")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "bulk_apply")
async def bulk_products_apply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    selected = [int(value) for value in data.get("bulk_selected_ids", [])]
    action = data.get("bulk_pending_action")
    if not selected or action not in {"shop_off", "shop_on", "api_off", "api_on"}:
        await callback.answer("Selection or action expired; please select again.", show_alert=True)
        await _show_bulk_manager(callback, state)
        return
    db = SessionLocal()
    try:
        products = db.query(Product).filter(Product.id.in_(selected)).all()
        for product in products:
            if action.startswith("shop_"):
                product.is_active = action == "shop_on"
            else:
                product.api_enabled = action == "api_on"
        db.commit()
        updated = len(products)
    except Exception:
        db.rollback()
        logger.exception("Bulk product visibility update failed")
        await callback.answer("Could not update products.", show_alert=True)
        return
    finally:
        db.close()
    invalidate_products_cache()
    await state.update_data(bulk_selected_ids=[], bulk_pending_action=None)
    await _show_bulk_manager(callback, state)
    await callback.answer(f"Updated {updated} product(s)", show_alert=True)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def safe(text: str) -> str:
    return html_escape(str(text or ""), quote=False)


def _divider(char: str = "━", length: int = 30) -> str:
    return char * length


async def _safe_edit_text(message: Message, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Safely edit message text, ignoring Telegram 'message is not modified' errors."""
    try:
        edited = await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        schedule_success_notice(edited, text)
        return edited
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return None
        raise


def _get_category_label(product) -> str:
    """Get formatted category label."""
    cats = {
        "premium": "Premium",
        "budget": "Budget",
        "vpn": "VPN",
        "email": "Email",
        "streaming": "Streaming",
        "gaming": "Gaming",
        "software": "Software",
        "education": "Education",
    }
    return cats.get((product.category or "").strip().lower(), (product.category or "General").strip() or "General")


def _build_stockctl_sync_text(product) -> str:
    """Build the stockctl sync notification text."""
    stock = _real_stock(product)

    status = "IN STOCK" if stock > 0 else "OUT OF STOCK"
    if 0 < stock <= 3:
        status = "LIMITED STOCK"

    lines = [
        "┌──(root㉿Rain)-[/inventory]",
        "└─# sudo stockctl sync",
        "Synchronizing inventory...",
        "[AUTH] Administrator Verified",
        "[SYNC] Stock Database Updated",
        "[INDEX] Repository Refreshed",
        "[LIVE] Marketplace Synchronized",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "STOCK UPDATED",
        f"Product     {product.name}",
        f"Category    {_get_category_label(product)}",
        f"Price       ${float(product.price or 0):.2f}",
        f"Stock       {stock} Available",
        f"State       {status}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Inventory synchronized.",
        "root@Rain:~#",
    ]
    return "<pre>" + safe("\n".join(lines)) + "</pre>"


async def _notify_stock_change(bot, pid: int):
    """Fetch product by ID, build stockctl sync notification, and send to channel."""
    product = await asyncio.to_thread(_load_product, pid)

    if not product:
        logger.warning("_notify_stock_change: Product %s not found", pid)
        return

    text = _build_stockctl_sync_text(product)
    stock = _real_stock(product)
    await _send_stock_channel_message(bot, text, product_id=product.id, can_buy=stock > 0)
    if stock > 0:
        await _notify_restock_subscribers(bot, product, stock)


def _load_product(pid: int):
    """Fetch a product in a worker thread; PyMySQL is synchronous."""
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if product is not None:
            db.expunge(product)
        return product
    finally:
        db.close()


def _parse_bulk_pricing(raw_text: str) -> dict | None:
    raw_text = raw_text.strip()

    if raw_text.lower() in ("skip", "none", "remove", "no", "n", ""):
        return None

    tiers = {}

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
            for k, v in tiers.items():
                if not isinstance(v, dict):
                    return None
                if "min" not in v or "price" not in v:
                    return None
            return tiers
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

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
            min_qty = int(range_part.replace("+", "").strip())
            tiers[str(min_qty)] = {"min": min_qty, "max": None, "price": price}
        elif "-" in range_part:
            parts = range_part.split("-")
            if len(parts) != 2:
                return None
            min_qty = int(parts[0].strip())
            max_qty = int(parts[1].strip())
            if min_qty >= max_qty:
                return None
            tiers[str(min_qty)] = {"min": min_qty, "max": max_qty, "price": price}
        else:
            return None

    return tiers if tiers else None


def _format_bulk_pricing_display(bulk_pricing: str | None) -> str:
    if not bulk_pricing:
        return ""

    try:
        tiers = json.loads(bulk_pricing)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not tiers:
        return ""

    lines = []
    sorted_tiers = sorted(tiers.values(), key=lambda x: x.get("min", 0))

    for tier in sorted_tiers:
        min_qty = tier.get("min", 1)
        max_qty = tier.get("max")
        price = tier.get("price", 0)
        if max_qty:
            lines.append(f"  🏷 {min_qty}-{max_qty} units → ${price:.2f}/each")
        else:
            lines.append(f"  🏷 {min_qty}+ units → ${price:.2f}/each")

    return "\n".join(lines)


def _format_bulk_pricing_plain(bulk_pricing: str | None) -> str:
    if not bulk_pricing:
        return "(not set)"

    try:
        tiers = json.loads(bulk_pricing)
    except (json.JSONDecodeError, TypeError):
        return "(invalid data)"

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


# ==================================================
# HELPER – REFRESH MANAGE PANEL
# ==================================================

async def _refresh_manage_panel(callback: CallbackQuery, pid: int):
    invalidate_products_cache()
    product = await asyncio.to_thread(_load_product, pid)

    if not product:
        await callback.answer("❌ Product not found.")
        return

    text, markup = _build_product_panel(product)
    await _safe_edit_text(callback.message, text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


def _build_product_panel(product: Product):
    delivery_type = (product.delivery_type or "automatic").lower()
    threshold = product.low_stock_threshold if product.low_stock_threshold is not None else DEFAULT_LOW_STOCK_THRESHOLD
    api_on = getattr(product, "api_enabled", True)
    description = product.description or "No description"
    preview = description[:180] + ("…" if len(description) > 180 else "")
    text = (
        f"{safe(product.icon or '📦')} <b>{safe(product.name)}</b>\n"
        f"🆔 #{product.id} · {'🟢 Enabled' if product.is_active else '🔴 Disabled'}\n"
        f"🏷 <b>Category:</b> {safe(product.category or 'General')}\n\n"
        f"💰 <b>Price:</b> ${float(product.price or 0):.2f} · 📊 <b>Stock:</b> {product.stock}\n"
        f"🚚 <b>Delivery:</b> {DELIVERY_LABELS.get(delivery_type, delivery_type)}\n"
        f"📦 <b>Preorder:</b> {'ON' if product.preorder else 'OFF'} · 🌐 <b>API:</b> {'ON' if api_on else 'OFF'}\n"
        f"⚠️ <b>Low-stock alert:</b> {threshold}\n"
        f"{safe(policy_label(product))}\n"
        f"📦 <b>Bulk pricing:</b> {'Configured' if product.bulk_pricing else 'Base price only'}\n"
    )
    reseller_id = getattr(product, 'reseller_service_id', None) or getattr(product, 'provider_product_id', None)
    reseller_name = getattr(product, 'reseller_name', None)
    reseller_cost = getattr(product, 'reseller_cost', None)
    if reseller_id or reseller_name:
        cost = f"${float(reseller_cost):.2f}" if reseller_cost is not None else 'N/A'
        text += (f"\n🏪 <b>Provider:</b> {safe(reseller_name or 'Configured Provider')}\n"
                 f"🆔 <b>Service:</b> <code>{safe(str(reseller_id or 'Not set'))}</code> · <b>Cost:</b> {cost}\n")
    text += f"\n📝 <b>Description:</b>\n{safe(preview)}\n\nChoose a tile below."

    from services.warranty import policy
    mode = policy(product)
    aftercare = (f"🛡 Warranty: {product.warranty_days}d" if mode == 'warranty' else
                 f"💸 Refund: {product.subscription_days}d" if mode == 'refund' else '🛡 Aftercare: OFF')
    has_accounts = bool(product.file_content and product.file_content.strip())

    def tile(label, action, style='primary'):
        return InlineKeyboardButton(text=label, callback_data=f'{action}_{product.id}', style=style)

    rows = [
        [tile('✏️ Name', 'edit_name'), tile('🏷 Category', 'edit_category')],
        [tile('💰 Price', 'edit_price'), tile('📦 Stock', 'edit_stock')],
        [tile('➕ Add Accounts', 'add_accounts', 'success'), tile('✏️ Edit Accounts' if has_accounts else '✏️ Accounts (empty)', 'edit_accounts')],
        [tile('🧾 Buyer Details', 'edit_required_info'), tile(aftercare, 'warranty_config')],
        [tile(DELIVERY_LABELS.get(delivery_type, delivery_type), 'cycle_delivery'), tile('Fallback: ON' if product.manual_fallback else 'Fallback: OFF', 'toggle_fallback')],
        [tile('🔴 Disable' if product.is_active else '🟢 Enable', 'toggle', 'danger' if product.is_active else 'success'), tile('🌐 API: ON' if api_on else '🌐 API: OFF', 'toggle_api')],
        [tile('📦 Preorder: ON' if product.preorder else '📦 Preorder: OFF', 'toggle_preorder'), tile('📦 Bulk Pricing', 'edit_bulk')],
        [tile('⚠️ Stock Alert', 'edit_threshold'), tile('📝 Description', 'desc')],
        [tile('🔗 Create Product Link', 'create_product_link'), tile('📅 Subscription term', 'ct_term')],
        [tile('🗑 Delete Product', 'delete_product', 'danger')],
        [InlineKeyboardButton(text='⬅ Back', callback_data='admin_products', style='primary')],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ==================================================
# COMMANDS & HANDLERS
# ==================================================

@router.message(Command("testadmin"))
async def test_admin(message: Message):
    await answer_card(message, "✅ Admin router working")


@router.callback_query(F.data.startswith("manage_"))
async def manage_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    product_id = int(callback.data.split("_")[1])

    product = await asyncio.to_thread(_load_product, product_id)

    if not product:
        await callback.answer("❌ Product not found.")
        return

    text, markup = _build_product_panel(product)
    await _safe_edit_text(callback.message, text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("create_product_link_"))
async def create_product_link(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    try:
        product_id = int(callback.data.removeprefix("create_product_link_"))
    except (TypeError, ValueError):
        await callback.answer("Invalid product.", show_alert=True)
        return

    product = await asyncio.to_thread(_load_product, product_id)
    if not product:
        await callback.answer("Product not found.", show_alert=True)
        return

    bot_info = await callback.bot.get_me()
    product_url = f"https://t.me/{bot_info.username}?start=product_{product_id}"
    await answer_card(callback,
        f"🔗 <b>Direct link for {safe(product.name)}</b>\n\n"
        f"<code>{product_url}</code>\n\n"
        "Share this link. When opened, it will take users directly to this product.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔎 Open Product", url=product_url)
        ]]),
        disable_web_page_preview=True,
    )
    await callback.answer("Product link created.")


# ==================================================
# ADD ACCOUNTS
# ==================================================

@router.callback_query(F.data.startswith("add_accounts_"))
async def add_accounts(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])
    await state.update_data(product_id=pid)
    await state.set_state(AddAccounts.accounts)

    await answer_card(callback,
        "Send accounts.\n\n"
        "One account per line.\n\n"
        "Example:\n"
        "email1@gmail.com:pass1\n"
        "email2@gmail.com:pass2\n\n"
        "Stock is recalculated from the total number of lines after each sale."
    )
    await callback.answer()


@router.message(AddAccounts.accounts)
async def save_accounts(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        account_text = await text_from_message(message)
    except ValueError as exc:
        await answer_card(message, f"❌ {html_escape(str(exc))}")
        return
    if account_text is None:
        await answer_card(message, "Send account lines as text or upload a .txt/.csv file.")
        return

    data = await state.get_data()
    pid = int(data["product_id"])
    raw_lines = [line.strip() for line in account_text.splitlines()]
    blank_count = sum(1 for line in raw_lines if not line)
    submitted = [line for line in raw_lines if line]
    deduplicated = list(dict.fromkeys(submitted))
    duplicate_input_count = len(submitted) - len(deduplicated)

    preview = await asyncio.to_thread(_preview_account_import, pid, deduplicated)
    if preview is None:
        await state.clear()
        await answer_card(message, "❌ Product not found.")
        return

    existing_duplicates = preview["existing_duplicates"]
    fresh_lines = preview["new_lines"]
    await state.update_data(
        pending_account_lines=fresh_lines,
        pending_account_product_id=pid,
        pending_account_input_count=len(submitted),
        pending_account_blank_count=blank_count,
        pending_account_duplicate_count=duplicate_input_count + existing_duplicates,
    )
    await state.set_state(AddAccounts.confirm)

    prompt = (
        f"📦 <b>STOCK IMPORT PREVIEW</b>\n\n"
        f"Product: <b>{html_escape(preview['product_name'])}</b> (#{pid})\n"
        f"Current stock: <b>{preview['current_stock']}</b>\n\n"
        f"Rows received: <b>{len(submitted)}</b>\n"
        f"Blank rows ignored: <b>{blank_count}</b>\n"
        f"Duplicates skipped: <b>{duplicate_input_count + existing_duplicates}</b>\n"
        f"New accounts to add: <b>{len(fresh_lines)}</b>\n"
        f"Projected stock: <b>{preview['current_stock'] + len(fresh_lines)}</b>\n\n"
        "Review the counts, then confirm. Account contents are not repeated in this preview."
    )
    await answer_card(
        message,
        prompt,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Add {len(fresh_lines)} accounts", callback_data="accounts_apply_import")],
            [InlineKeyboardButton(text="❌ Cancel import", callback_data="accounts_cancel_import")],
        ]),
    )


def _preview_account_import(product_id: int, lines: list[str]) -> dict | None:
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            return None
        existing = {line.strip() for line in (product.file_content or "").splitlines() if line.strip()}
        new_lines = [line for line in lines if line not in existing]
        return {
            "product_name": product.name,
            "current_stock": int(product.stock or 0),
            "new_lines": new_lines,
            "existing_duplicates": len(lines) - len(new_lines),
        }
    finally:
        db.close()


def _commit_account_import(product_id: int, lines: list[str], admin_id: int) -> dict:
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product:
            return {"error": "Product not found."}
        existing_lines = [line.strip() for line in (product.file_content or "").splitlines() if line.strip()]
        existing = set(existing_lines)
        fresh = [line for line in lines if line and line not in existing]
        product.file_content = "\n".join(existing_lines + fresh)
        product.stock = max(product.stock or 0, len(existing_lines) + len(fresh)) if manual_allowed(product) else len(existing_lines) + len(fresh)
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id),
            action="add_stock_accounts",
            entity_type="product",
            entity_id=str(product.id),
            summary=f"Added {len(fresh)} account(s); stock is now {product.stock}.",
        ))
        db.commit()
        return {"added": len(fresh), "stock": product.stock}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.callback_query(F.data == "accounts_apply_import")
async def accounts_apply_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    pid = int(data.get("pending_account_product_id") or 0)
    lines = data.get("pending_account_lines") or []
    if not pid or not lines:
        await callback.answer("No new account rows to import.", show_alert=True)
        return
    try:
        result = await asyncio.to_thread(_commit_account_import, pid, lines, callback.from_user.id)
    except Exception:
        logger.exception("Bulk account import failed for product %s", pid)
        await callback.answer("Import failed. No changes were saved.", show_alert=True)
        return
    if result.get("error"):
        await callback.answer(result["error"], show_alert=True)
        return
    await state.clear()
    invalidate_products_cache()
    await callback.answer("Stock updated.")
    try:
        await _notify_stock_change(callback.bot, pid)
    except Exception:
        logger.exception("Failed to notify after stock import")
    await callback.message.edit_text(
        f"✅ <b>STOCK UPDATED</b>\n\nAdded: <b>{result['added']}</b>\nCurrent stock: <b>{result['stock']}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Product Manager", callback_data="admin_products")],
        ]),
    )


@router.callback_query(F.data == "accounts_cancel_import")
async def accounts_cancel_import(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Import cancelled.")
    await callback.message.edit_text(
        "❌ <b>Stock import cancelled.</b> No accounts were added.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Product Manager", callback_data="admin_products")],
        ]),
    )


# ==================================================
# EDIT ACCOUNTS
# ==================================================

@router.callback_query(F.data.startswith("edit_accounts_"))
async def edit_accounts(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])

    product = await asyncio.to_thread(_load_product, pid)

    if not product:
        await callback.answer("❌ Product not found.")
        return

    accounts = product.file_content or ""
    line_count = len([l for l in accounts.splitlines() if l.strip()])
    preview = accounts[:500] + ("..." if len(accounts) > 500 else "")

    text = f"📋 <b>Accounts for {safe(product.name)}</b>\n\n"
    text += f"📊 Total accounts: <b>{line_count}</b>\n"
    text += f"📦 Current stock: <b>{product.stock}</b>\n\n"
    text += f"<code>{safe(preview)}</code>"

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Replace All (overwrite)",
                    callback_data=f"replace_accounts_{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗑 Clear All Accounts",
                    callback_data=f"clear_accounts_{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Add More",
                    callback_data=f"add_accounts_{pid}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back to Product",
                    callback_data=f"manage_{pid}"
                )
            ]
        ]
    )

    await _safe_edit_text(callback.message, text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("replace_accounts_"))
async def replace_accounts_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])
    await state.update_data(product_id=pid, replace_mode=True)
    await state.set_state(EditAccounts.replace_accounts)

    await answer_card(callback,
        "📝 Send the <b>new</b> account list.\n\n"
        "This will <b>replace</b> all existing accounts.\n"
        "One account per line.\n\n"
        "Example:\n"
        "email1@gmail.com:pass1\n"
        "email2@gmail.com:pass2",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(EditAccounts.replace_accounts)
async def replace_accounts_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    try:
        account_text = await text_from_message(message)
    except ValueError as exc:
        await answer_card(message, f"❌ {html_escape(str(exc))}")
        return
    if account_text is None:
        await answer_card(message, "Send account lines as text or upload a .txt/.csv file.")
        return

    data = await state.get_data()
    pid = int(data["product_id"])
    raw_lines = [line.strip() for line in account_text.splitlines()]
    blank_count = sum(1 for line in raw_lines if not line)
    submitted = [line for line in raw_lines if line]
    unique_lines = list(dict.fromkeys(submitted))
    preview = await asyncio.to_thread(_preview_account_import, pid, unique_lines)
    if preview is None:
        await state.clear()
        await answer_card(message, "❌ Product not found.")
        return

    await state.update_data(
        pending_replace_lines=unique_lines,
        pending_replace_product_id=pid,
        pending_replace_input_count=len(submitted),
        pending_replace_blank_count=blank_count,
        pending_replace_duplicate_count=len(submitted) - len(unique_lines),
        pending_replace_old_stock=preview["current_stock"],
    )
    await state.set_state(EditAccounts.confirm_replace)
    await answer_card(
        message,
        f"⚠️ <b>REPLACE STOCK PREVIEW</b>\n\n"
        f"Product: <b>{html_escape(preview['product_name'])}</b> (#{pid})\n"
        f"Current stock that will be removed: <b>{preview['current_stock']}</b>\n"
        f"New unique accounts: <b>{len(unique_lines)}</b>\n"
        f"Blank rows ignored: <b>{blank_count}</b>\n"
        f"Duplicate rows skipped: <b>{len(submitted) - len(unique_lines)}</b>\n\n"
        "This replaces the current account pool. Account contents are hidden from this preview. Confirm to continue.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚠️ Replace with {len(unique_lines)} accounts", callback_data="accounts_apply_replace")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="accounts_cancel_replace")],
        ]),
    )


@router.callback_query(F.data == "accounts_apply_replace")
async def accounts_apply_replace(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    pid = int(data.get("pending_replace_product_id") or 0)
    lines = data.get("pending_replace_lines") or []
    if not pid or not lines:
        await callback.answer("Replacement data expired or empty.", show_alert=True)
        return
    try:
        result = await asyncio.to_thread(_commit_account_replacement, pid, lines, callback.from_user.id)
    except Exception:
        logger.exception("Account replacement failed for product %s", pid)
        await callback.answer("Replacement failed. No changes were saved.", show_alert=True)
        return
    if result.get("error"):
        await callback.answer(result["error"], show_alert=True)
        return
    await state.clear()
    invalidate_products_cache()
    await callback.answer("Account pool replaced.")
    try:
        await _notify_stock_change(callback.bot, pid)
    except Exception:
        logger.exception("Failed to send stock notification after replacing accounts")
    await callback.message.edit_text(
        f"✅ <b>ACCOUNT POOL REPLACED</b>\n\nNew stock: <b>{result['stock']}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Back to Product", callback_data=f"manage_{pid}")],
        ]),
    )


def _commit_account_replacement(product_id: int, lines: list[str], admin_id: int) -> dict:
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product:
            return {"error": "Product not found."}
        previous_count = len([line for line in (product.file_content or "").splitlines() if line.strip()])
        product.file_content = "\n".join(lines)
        product.stock = max(product.stock or 0, len(lines)) if manual_allowed(product) else len(lines)
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id), action="replace_stock_accounts",
            entity_type="product", entity_id=str(product.id),
            summary=f"Replaced {previous_count} account(s) with {product.stock}.",
        ))
        db.commit()
        return {"stock": product.stock}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.callback_query(F.data == "accounts_cancel_replace")
async def accounts_cancel_replace(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Replacement cancelled.")
    await callback.message.edit_text(
        "❌ <b>Replacement cancelled.</b> Existing stock was unchanged.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Product Manager", callback_data="admin_products")],
        ]),
    )


@router.callback_query(F.data.startswith("clear_accounts_"))
async def clear_accounts(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if not product:
            await callback.answer("❌ Product not found.")
            return

        product.file_content = None
        product.stock = product.stock if manual_allowed(product) else 0
        db.commit()
        invalidate_products_cache()
        db.refresh(product)
    finally:
        db.close()

    try:
        await _notify_stock_change(callback.bot, pid)
    except Exception:
        logger.exception("Failed to send stock notification after clearing accounts")

    await callback.answer("🗑 Accounts cleared.")
    await _refresh_manage_panel(callback, pid)


# ==================================================
# ENABLE / DISABLE & TOGGLES
# ==================================================

@router.callback_query(
    F.data.startswith("toggle_")
    & ~F.data.startswith("toggle_preorder_")
    & ~F.data.startswith("toggle_api_")
)
async def toggle_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[1])

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if product:
            product.is_active = not product.is_active
            db.commit()
    finally:
        db.close()

    await _refresh_manage_panel(callback, pid)


@router.callback_query(F.data.startswith("toggle_api_"))
async def toggle_product_api(callback: CallbackQuery):
    """Enable or disable this product for public API customers only."""
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if not product:
            await callback.answer("❌ Product not found.", show_alert=True)
            return
        product.api_enabled = not bool(getattr(product, "api_enabled", True))
        db.commit()
        invalidate_products_cache()
        api_enabled = product.api_enabled
    finally:
        db.close()

    await callback.answer(
        "🌐 API enabled for this product"
        if api_enabled
        else "🌐 API disabled for this product"
    )
    await _refresh_manage_panel(callback, pid)


@router.callback_query(F.data.startswith("cycle_delivery_"))
async def cycle_delivery_type(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if not product:
            await callback.answer("❌ Product not found.")
            return

        current = (product.delivery_type or "automatic").lower()
        try:
            next_index = (DELIVERY_TYPES.index(current) + 1) % len(DELIVERY_TYPES)
        except ValueError:
            next_index = 0
        product.delivery_type = DELIVERY_TYPES[next_index]
        db.commit()
        invalidate_products_cache()

        new_label = DELIVERY_LABELS[product.delivery_type]
    finally:
        db.close()

    await callback.answer(f"🚚 Delivery set to {new_label}")
    await _refresh_manage_panel(callback, pid)


@router.callback_query(F.data.startswith("toggle_fallback_"))
async def toggle_manual_fallback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = int(callback.data.rsplit("_", 1)[1])
    def save():
        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == pid).with_for_update().first()
            if not product or product.source != "own":
                return False
            product.manual_fallback = not product.manual_fallback
            db.commit()
            return True
        finally:
            db.close()
    if not await asyncio.to_thread(save):
        await callback.answer("Local product not found.", show_alert=True)
        return
    invalidate_products_cache()
    await callback.answer("Manual fallback updated.")
    await _refresh_manage_panel(callback, pid)


@router.callback_query(F.data.startswith("toggle_preorder_"))
async def toggle_preorder(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if not product:
            await callback.answer("❌ Product not found.")
            return

        product.preorder = not product.preorder
        db.commit()
        invalidate_products_cache()
        new_state = product.preorder
    finally:
        db.close()

    await callback.answer("📦 Preorder enabled" if new_state else "📦 Preorder disabled")
    await _refresh_manage_panel(callback, pid)


# ==================================================
# BULK PRICING
# ==================================================

@router.callback_query(F.data.startswith("edit_bulk_"))
async def edit_bulk_pricing(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    pid = int(callback.data.split("_")[2])

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
    finally:
        db.close()

    if not product:
        await callback.answer("❌ Product not found.")
        return

    current = _format_bulk_pricing_plain(product.bulk_pricing)

    await state.update_data(edit_bulk_pid=pid)
    await state.set_state(EditBulkPricing.waiting_input)

    text = (
        "╔══════════════════════════════╗\n"
        "║  📦 EDIT BULK PRICING       ║\n"
        "╚══════════════════════════════╝\n\n"
        f"📦 <b>Product:</b> #{pid} — {safe(product.name)}\n\n"
        f"{_divider('─')}\n\n"
        f"<b>Current Bulk Pricing:</b>\n"
        f"<code>{safe(current)}</code>\n\n"
        f"{_divider('═')}\n\n"
        f"📝 <b>Send new tiers</b> (one per line):\n\n"
        f"<code>1-10=5.00</code>\n"
        f"  └ 1-10 units → $5.00 each\n\n"
        f"<code>11-50=4.00</code>\n"
        f"  └ 11-50 units → $4.00 each\n\n"
        f"<code>51+=3.00</code>\n"
        f"  └ 51+ units → $3.00 each\n\n"
        f"{_divider('─')}\n\n"
        f"💡 Send <b>skip</b> or <b>remove</b> to\n"
        f"delete bulk pricing (flat pricing)"
    )

    await answer_card(callback, text, parse_mode="HTML")
    await callback.answer()


@router.message(EditBulkPricing.waiting_input)
async def save_bulk_pricing(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    if not message.text:
        return

    data = await state.get_data()
    pid = data.get("edit_bulk_pid")
    raw = message.text.strip()

    if raw.lower() in ("skip", "none", "remove", "no", "n"):
        bulk_json = None
        confirm = (
            "✅ <b>Bulk Pricing Removed!</b>\n\n"
            "📦 Now using flat pricing.\n"
            "All quantities will be charged at the base price."
        )
        await state.clear()
        await answer_card(message,
            confirm,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 Back to Product", callback_data=f"manage_{pid}")]]
            )
        )
    else:
        bulk_data = _parse_bulk_pricing(raw)

        if bulk_data is None:
            await answer_card(message,
                "❌ <b>Invalid format!</b>\n\n"
                f"{_divider('─')}\n\n"
                "Please use:\n\n"
                "<code>1-10=5.00</code>\n"
                "<code>11-50=4.00</code>\n"
                "<code>51+=3.00</code>\n\n"
                "OR send <b>skip</b> to remove.",
                parse_mode="HTML"
            )
            return

        bulk_json = json.dumps(bulk_data)
        display = _format_bulk_pricing_display(bulk_json)

        await state.clear()

        confirm = (
            "✅ <b>Bulk Pricing Updated!</b>\n\n"
            f"{_divider('─')}\n"
            f"{display}\n"
            f"{_divider('─')}\n\n"
            "📦 Buyers will automatically get\n"
            "the best price for their quantity!"
        )

        await answer_card(message,
            confirm,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 Back to Product", callback_data=f"manage_{pid}")]]
            )
        )

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if product:
            product.bulk_pricing = bulk_json
            db.commit()
            invalidate_products_cache()
    finally:
        db.close()


# ==================================================
# QUICK PROMPTS & COMMAND EDITORS
# ==================================================

@router.callback_query(F.data.startswith("edit_name_"))
async def edit_product_name_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        pid = int(callback.data.removeprefix("edit_name_"))
    except (TypeError, ValueError):
        await callback.answer("Invalid product.", show_alert=True)
        return

    product = await asyncio.to_thread(_load_product, pid)
    if not product:
        await callback.answer("Product not found.", show_alert=True)
        return

    await state.update_data(edit_product_name_id=pid)
    await state.set_state(EditProduct.name)
    await answer_card(callback,
        f"Current name: <b>{safe(product.name)}</b>\n\n"
        "Send the new product name (1–255 characters).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="cancel_edit_name")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_edit_name")
async def cancel_product_name_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    pid = data.get("edit_product_name_id")
    await state.clear()
    if pid:
        await answer_card(callback,
            "Name edit cancelled.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📋 Back to Product", callback_data=f"manage_{pid}")]]
            ),
        )
    else:
        await answer_card(callback, "Name edit cancelled.")
    await callback.answer()


@router.message(EditProduct.name)
async def save_product_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name:
        await answer_card(message, "❌ Product name can't be empty. Send a name or tap Cancel.")
        return
    if len(name) > 255:
        await answer_card(message, "❌ Product name must be 255 characters or fewer.")
        return

    data = await state.get_data()
    pid = data.get("edit_product_name_id")
    if not pid:
        await state.clear()
        await answer_card(message, "❌ Product edit expired. Open the product manager and try again.")
        return

    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid)).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        product.name = name
        db.commit()
        invalidate_products_cache()
    except Exception:
        db.rollback()
        logger.exception("Failed to update product name for product %s", pid)
        await answer_card(message, "❌ Could not update the product name. Please try again.")
        return
    finally:
        db.close()
        await state.clear()

    product = await asyncio.to_thread(_load_product, int(pid))
    if product:
        panel_text, markup = _build_product_panel(product)
        await answer_card(message, panel_text, reply_markup=markup, parse_mode="HTML")
    else:
        await answer_card(message, "✅ Product name updated.")


@router.callback_query(F.data.startswith("edit_category_"))
async def edit_product_category_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        pid = int(callback.data.removeprefix("edit_category_"))
    except (TypeError, ValueError):
        await callback.answer("Invalid product.", show_alert=True)
        return
    product = await asyncio.to_thread(_load_product, pid)
    if not product:
        await callback.answer("Product not found.", show_alert=True)
        return
    await state.update_data(edit_product_category_id=pid)
    await state.set_state(EditProduct.category)
    await answer_card(
        callback,
        f"Current category: <b>{safe(product.category or 'General')}</b>\n\n"
        "Send the category name (up to 255 characters). You can type a new category, "
        "or send <code>General</code> to place it in General.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Cancel", callback_data="cancel_edit_category")
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_edit_category")
async def cancel_product_category_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await state.get_data()
    pid = data.get("edit_product_category_id")
    await state.clear()
    if pid:
        await _refresh_manage_panel(callback, int(pid))
    else:
        await callback.answer("Category edit cancelled.")


@router.message(EditProduct.category)
async def save_product_category(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    category = (message.text or "").strip()
    if not category:
        await answer_card(message, "❌ Category cannot be empty. Send a category name or tap Cancel.")
        return
    if len(category) > 255:
        await answer_card(message, "❌ Category must be 255 characters or fewer.")
        return
    data = await state.get_data()
    pid = data.get("edit_product_category_id")
    if not pid:
        await state.clear()
        await answer_card(message, "❌ Product edit expired. Reopen the product manager and try again.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid)).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        product.category = category
        db.commit()
        invalidate_products_cache()
    except Exception:
        db.rollback()
        logger.exception("Failed to update category for product %s", pid)
        await answer_card(message, "❌ Could not update the product category. Please try again.")
        return
    finally:
        db.close()
        await state.clear()
    product = await asyncio.to_thread(_load_product, int(pid))
    if product:
        panel_text, markup = _build_product_panel(product)
        await answer_card(message, panel_text, reply_markup=markup, parse_mode="HTML")
    else:
        await answer_card(message, "✅ Product category updated.")

@router.callback_query(F.data.startswith("edit_required_info_"))
async def edit_required_info_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = int(callback.data.removeprefix("edit_required_info_"))
    product = await asyncio.to_thread(_load_product, pid)
    if not product:
        await callback.answer("Product not found.", show_alert=True)
        return
    await state.update_data(edit_required_info_pid=pid)
    await state.set_state(EditProduct.required_info)
    await answer_card(callback,
        "Send one buyer requirement per line, or <code>skip</code> to remove them.\n\n"
        "<code>email:Email address</code>\n<code>phone:Contact number</code>\n"
        "<code>photo:Upload a screenshot</code>\n<code>contact:Share Telegram contact</code>\n"
        "<code>text:Other information</code>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Cancel", callback_data="cancel_edit_required_info")]]))
    await callback.answer()


@router.callback_query(F.data == "cancel_edit_required_info")
async def cancel_required_info_edit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pid = data.get("edit_required_info_pid")
    await state.clear()
    if pid:
        await _refresh_manage_panel(callback, int(pid))
    else:
        await callback.answer("Edit cancelled.")


@router.message(EditProduct.required_info)
async def save_required_info_edit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    pid = data.get("edit_required_info_pid")
    raw = (message.text or "").strip()
    requirements = []
    if raw.lower() not in {"skip", "none", "no", "n"}:
        allowed = {"email", "phone", "photo", "contact", "text"}
        for line in raw.splitlines():
            if not line.strip():
                continue
            kind, sep, label = line.strip().partition(":")
            kind = kind.strip().lower()
            if kind not in allowed:
                await answer_card(message, "❌ Allowed types: email, phone, photo, contact, text. Example: <code>email:Email address</code>", parse_mode="HTML")
                return
            requirements.append({"type": kind, "label": (label.strip() if sep and label.strip() else kind.title())[:100]})
    if len(requirements) > 8:
        await answer_card(message, "❌ Maximum 8 fields.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid)).first() if pid else None
        if not product:
            await answer_card(message, "❌ Product edit expired or product not found.")
            return
        product.required_info = requirements or None
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed saving buyer requirements for product %s", pid)
        await answer_card(message, "❌ Could not save buyer requirements.")
        return
    finally:
        db.close()
        await state.clear()
    product = await asyncio.to_thread(_load_product, int(pid))
    if product:
        panel_text, markup = _build_product_panel(product)
        await answer_card(message, panel_text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("edit_price_"))
async def edit_price(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = callback.data.split("_")[2]
    await answer_card(callback, f"Send:\n<code>/setprice {pid} 9.99</code>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("edit_stock_"))
async def edit_stock(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = callback.data.split("_")[2]
    await answer_card(callback, f"Send:\n<code>/setstock {pid} 100</code>", parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("edit_threshold_"))
async def edit_threshold(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = callback.data.split("_")[2]
    await answer_card(callback,
        f"Send:\n<code>/setthreshold {pid} 3</code>\n\n"
        "You'll get a Telegram alert whenever stock drops to or below this number.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("desc_"))
async def edit_desc(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = callback.data.split("_")[1]
    await answer_card(callback, f"Send:\n<code>/setdesc {pid} New Description</code>", parse_mode="HTML")
    await callback.answer()


# ==================================================
# SET PRICE / SET STOCK / SET THRESHOLD / SET DESC
# ==================================================

@router.message(Command("setprice"))
async def set_price(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await answer_card(message, "❌ Usage: <code>/setprice &lt;product_id&gt; &lt;price&gt;</code>\nExample: <code>/setprice 5 9.99</code>", parse_mode="HTML")
        return
    _, pid_raw, price_raw = parts
    if not pid_raw.isdigit():
        await answer_card(message, "❌ Product ID must be a number.")
        return
    try:
        price = float(price_raw)
    except ValueError:
        await answer_card(message, "❌ Price must be a number, e.g. 9.99")
        return
    if price < 0:
        await answer_card(message, "❌ Price can't be negative.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid_raw)).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        previous_price = str(product.price)
        product.price = Decimal(str(price))
        db.add(AdminAuditLog(
            admin_telegram_id=int(message.from_user.id), action="set_product_price",
            entity_type="product", entity_id=str(product.id),
            summary=f"Price changed from ${previous_price} to ${price:.2f}.",
        ))
        db.commit()
        invalidate_products_cache()
        await answer_card(message, f"✅ Price updated to <b>${price:.2f}</b>.", parse_mode="HTML")
    finally:
        db.close()


@router.message(Command("setstock"))
async def set_stock(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await answer_card(message, "❌ Usage: <code>/setstock &lt;product_id&gt; &lt;stock&gt;</code>\nExample: <code>/setstock 5 100</code>", parse_mode="HTML")
        return
    _, pid_raw, stock_raw = parts
    if not pid_raw.isdigit():
        await answer_card(message, "❌ Product ID must be a number.")
        return
    pid = int(pid_raw)
    try:
        stock = int(stock_raw)
    except ValueError:
        await answer_card(message, "❌ Stock must be a whole number.")
        return
    if stock < 0:
        await answer_card(message, "❌ Stock can't be negative.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        delivery_type = (product.delivery_type or "automatic").lower()
        if not manual_allowed(product) and not getattr(product, "reseller_service_id", None):
            await answer_card(message,
                "ℹ️ This product delivers automatically from loaded accounts, "
                "so stock is derived from the account list. Setting manual stock "
                "requires enabling Manual fallback in the product first."
            )
            return
        previous_stock = int(product.stock or 0)
        product.stock = stock
        db.add(AdminAuditLog(
            admin_telegram_id=int(message.from_user.id), action="set_product_stock",
            entity_type="product", entity_id=str(product.id),
            summary=f"Stock changed from {previous_stock} to {stock}.",
        ))
        db.commit()
        invalidate_products_cache()
        db.refresh(product)
    finally:
        db.close()

    try:
        await _notify_stock_change(message.bot, pid)
    except Exception:
        logger.exception("Failed to send stock notification after /setstock")

    await answer_card(message, f"✅ Stock updated to <b>{stock}</b>.", parse_mode="HTML")


@router.message(Command("setthreshold"))
async def set_threshold(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return
    parts = message.text.split()
    if len(parts) != 3:
        await answer_card(message, "❌ Usage: <code>/setthreshold &lt;product_id&gt; &lt;threshold&gt;</code>\nExample: <code>/setthreshold 5 3</code>", parse_mode="HTML")
        return
    _, pid_raw, threshold_raw = parts
    if not pid_raw.isdigit():
        await answer_card(message, "❌ Product ID must be a number.")
        return
    try:
        threshold = int(threshold_raw)
    except ValueError:
        await answer_card(message, "❌ Threshold must be a whole number.")
        return
    if threshold < 0:
        await answer_card(message, "❌ Threshold can't be negative.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid_raw)).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        previous_threshold = product.low_stock_threshold
        product.low_stock_threshold = threshold
        db.add(AdminAuditLog(
            admin_telegram_id=int(message.from_user.id), action="set_low_stock_threshold",
            entity_type="product", entity_id=str(product.id),
            summary=f"Low-stock threshold changed from {previous_threshold} to {threshold}.",
        ))
        db.commit()
        invalidate_products_cache()
        await answer_card(message, f"✅ Low stock alert threshold set to <b>{threshold}</b>.", parse_mode="HTML")
    finally:
        db.close()


@router.message(Command("setdesc"))
async def set_desc(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await answer_card(message, "❌ Usage: <code>/setdesc &lt;product_id&gt; &lt;description&gt;</code>", parse_mode="HTML")
        return
    _, pid_raw, desc = parts
    if not pid_raw.isdigit():
        await answer_card(message, "❌ Product ID must be a number.")
        return
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == int(pid_raw)).first()
        if not product:
            await answer_card(message, "❌ Product not found.")
            return
        product.description = desc
        db.commit()
        invalidate_products_cache()
        await answer_card(message, "✅ Description updated.")
    finally:
        db.close()


# ==================================================
# DELETE PRODUCT
# ==================================================

@router.callback_query(F.data.startswith("delete_product_"))
async def delete_product(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return
    pid = int(callback.data.split("_")[2])
    db = SessionLocal()
    try:
        product = db.query(Product).filter(Product.id == pid).first()
        if product:
            db.delete(product)
            db.commit()
            invalidate_products_cache()
    finally:
        db.close()

    await _safe_edit_text(
        callback.message,
        "✅ Product deleted.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📦 Product Manager",
                        callback_data="admin_products"
                    )
                ]
            ]
        ),
        parse_mode="HTML"
    )
    await callback.answer()
