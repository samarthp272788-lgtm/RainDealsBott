
from utils.ui import answer_card
import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import SessionLocal
from handlers.products import (
    _fetch_active_products,
    _get_catalog_custom_prices,
    _get_category_config,
    _real_stock,
    _refresh_reseller_stock_cache_if_needed,
)
from handlers.start import _build_audit_profile, _format_balance
from models.order import Order
from models.user import User

router = Router()


@router.message(Command("shop"))
async def shop_command(message: Message):
    products = await _fetch_active_products()
    if not products:
        await answer_card(message, "📭 No products are available right now.")
        return

    if any((getattr(product, "source", "own") or "own") == "reseller" for product in products):
        asyncio.create_task(_refresh_reseller_stock_cache_if_needed())

    prices = await asyncio.to_thread(
        _get_catalog_custom_prices, message.from_user.id, [product.id for product in products]
    )
    buttons = []
    for product in products[:20]:
        category = _get_category_config(product.category)
        price = prices.get(product.id, product.price)
        price_label = "FREE" if float(price) == 0 else f"${price:.2f}"
        stock = _real_stock(product)
        stock_text = "OOS" if stock <= 0 else ("∞" if stock >= 999999 else str(stock))
        buttons.append([InlineKeyboardButton(
            text=f"#{product.id} {product.icon or category['icon']} {product.name[:28]} · {price_label} · {stock_text}",
            callback_data=f"product_{product.id}",
        )])
    if len(products) > 20:
        buttons.append([InlineKeyboardButton(text="🛍 Open full catalog", callback_data="products_menu")])
    await answer_card(message,
        "🛍 <b>Available products</b>\nSelect a product to view details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(Command("wallet"))
async def wallet_command(message: Message):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
        if not user:
            await answer_card(message, "Please send /start first to create your account.")
            return
        balance = _format_balance(user.balance)
        deposited = _format_balance(user.total_deposited)
        text = (
            "💰 <b>YOUR WALLET</b>\n\n"
            f"Available balance: <b>${balance}</b>\n"
            f"Total deposited: <b>${deposited}</b>"
        )
    finally:
        db.close()

    await answer_card(message,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Deposit", callback_data="deposit_start")],
            [InlineKeyboardButton(text="📜 Deposit History", callback_data="my_deposits")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
        ]),
    )


@router.message(Command("orders"))
async def orders_command(message: Message):
    db = SessionLocal()
    try:
        orders = (db.query(Order)
                  .filter(Order.telegram_id == message.from_user.id)
                  .order_by(Order.id.desc()).limit(10).all())
        if not orders:
            await answer_card(message, "📭 <b>No recent orders found.</b>", parse_mode="HTML")
            return
        buttons = [[InlineKeyboardButton(
            text=f"#{order.id} · {escape(str(order.product_name or 'Product'))[:28]} · ${float(order.amount):.2f}",
            callback_data=f"order_detail_{order.id}",
        )] for order in orders]
    finally:
        db.close()
    await answer_card(message,
        "📦 <b>Your recent orders</b>\nSelect an order to view its details:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(Command("profile"))
async def profile_command(message: Message):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == message.from_user.id).first()
        if not user:
            await answer_card(message, "Please send /start first to create your account.")
            return
        text = _build_audit_profile(user, message.from_user.id)
    finally:
        db.close()
    await answer_card(message,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Back", callback_data="profile_back")
        ]]),
    )
