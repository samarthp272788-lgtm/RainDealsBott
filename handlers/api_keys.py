"""Telegram UI for users to create and revoke their reseller API key."""

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from services.api_keys import (
    ApiKeyConfigurationError,
    active_api_key_with_secret,
    create_api_key,
    revoke_api_key,
)
from utils.ui import show


router = Router()

API_ROOT = "https://raindeals-api.samarthp2727-rd.workers.dev"
API_BASE = f"{API_ROOT}/api/v1"
API_DOCS = f"{API_ROOT}/docs/"


def _api_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Generate / Rotate API Key",
                    callback_data="api_key_generate",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛑 Revoke API Key",
                    callback_data="api_key_revoke",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Main Menu",
                    callback_data="main_menu",
                )
            ],
        ]
    )


def _api_endpoints() -> str:
    return (
        "🧭 <b>API endpoints</b>\n"
        "• <code>GET /api/v1/me</code> — balance and account\n"
        "• <code>GET /api/v1/products</code> — list products\n"
        "• <code>POST /api/v1/order</code> — place an order\n"
        "• <code>GET /api/v1/orders</code> — order history\n"
        "• <code>GET /api/v1/order/{id}</code> — order details\n"
    )


@router.callback_query(F.data == "api_key_menu")
async def api_key_menu(callback: CallbackQuery):
    await callback.answer()

    try:
        key, raw_key = await asyncio.to_thread(
            active_api_key_with_secret,
            callback.from_user.id,
        )
    except ApiKeyConfigurationError:
        await show(
            callback,
            "⚠️ <b>API Access Not Ready</b>\n\n"
            "The administrator must configure <code>API_KEY_PEPPER</code> first.",
            parse_mode="HTML",
            reply_markup=_api_menu(),
        )
        return

    if raw_key:
        status = f"✅ <b>Your active API key</b>\n<code>{escape(raw_key)}</code>"
    elif key:
        status = (
            f"✅ Active key: <code>{escape(key.key_prefix)}...</code>\n"
            "⚠️ This older key cannot be displayed again. Generate a new key "
            "to view it here."
        )
    else:
        status = "❌ No active API key"

    await show(
        callback,
        "🔑 <b>RESELLER API ACCESS</b>\n\n"
        f"{status}\n\n"
        "Use your key from your website's server. Never put it in browser "
        "JavaScript, HTML, screenshots, or GitHub. Requests use your wallet "
        "balance, custom rates, available stock, and automatic delivery.\n\n"
        f'🌐 Production API: <a href="{API_ROOT}/">Open API</a>\n'
        f'📚 <a href="{API_DOCS}">API documentation and access guide</a>\n'
        f"🔗 Base URL: <code>{API_BASE}</code>\n\n"
        f"{_api_endpoints()}\n"
        "Generating a new key immediately revokes your previous key.",
        parse_mode="HTML",
        reply_markup=_api_menu(),
    )


@router.callback_query(F.data == "api_key_generate")
async def api_key_generate(callback: CallbackQuery):
    await callback.answer()

    try:
        raw_key, _ = await asyncio.to_thread(
            create_api_key,
            callback.from_user.id,
        )
    except ApiKeyConfigurationError:
        await show(
            callback,
            "⚠️ <b>API Access Not Ready</b>\n\n"
            "The administrator must add <code>API_KEY_PEPPER</code> "
            "to the server environment first.",
            parse_mode="HTML",
            reply_markup=_api_menu(),
        )
        return

    await show(
        callback,
        "✅ <b>NEW API KEY CREATED</b>\n\n"
        "You can open API Access anytime to view this active key again.\n\n"
        f"<code>{escape(raw_key)}</code>\n\n"
        "🔒 Store it as a server secret. Do not put it in browser JavaScript, "
        "HTML, screenshots, or GitHub.\n\n"
        f'📚 <a href="{API_DOCS}">API documentation and access guide</a>\n'
        f"Base URL: <code>{API_BASE}</code>",
        parse_mode="HTML",
        reply_markup=_api_menu(),
    )


@router.callback_query(F.data == "api_key_revoke")
async def api_key_revoke(callback: CallbackQuery):
    await callback.answer()
    revoked = await asyncio.to_thread(revoke_api_key, callback.from_user.id)

    if revoked:
        text = (
            "🛑 <b>API Key Revoked</b>\n\n"
            "That key can no longer access your balance or products."
        )
    else:
        text = "ℹ️ <b>No Active API Key</b>\n\nThere was nothing to revoke."

    await show(callback, text, parse_mode="HTML", reply_markup=_api_menu())
