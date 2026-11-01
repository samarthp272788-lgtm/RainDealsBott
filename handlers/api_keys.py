"""Telegram UI for users to create and revoke their Wasmer reseller API key."""

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import API_BASE_URL
from services.api_keys import ApiKeyConfigurationError, active_api_key_with_secret, create_api_key, revoke_api_key
from utils.ui import show


router = Router()


def _api_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Generate / Rotate API Key", callback_data="api_key_generate")],
        [InlineKeyboardButton(text="🛑 Revoke API Key", callback_data="api_key_revoke")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
    ])


def _base_url() -> str:
    return API_BASE_URL or "Your configured API domain"


@router.callback_query(F.data == "api_key_menu")
async def api_key_menu(callback: CallbackQuery):
    await callback.answer()
    try:
        key, raw_key = await asyncio.to_thread(active_api_key_with_secret, callback.from_user.id)
    except ApiKeyConfigurationError:
        await show(callback, "⚠️ <b>API Access Not Ready</b>\n\nThe administrator must configure API_KEY_PEPPER first.", parse_mode="HTML", reply_markup=_api_menu())
        return
    if raw_key:
        status = f"✅ <b>Your active API key</b>\n<code>{escape(raw_key)}</code>"
    elif key:
        status = (
            f"✅ Active key: <code>{escape(key.key_prefix)}...</code>\n"
            "⚠️ This older key was created before secure key display was enabled. "
            "Generate a new key once to show it here anytime."
        )
    else:
        status = "❌ No active API key"
    await show(
        callback,
        "🔑 <b>RESELLER API ACCESS</b>\n\n"
        f"{status}\n\n"
        "Use this API through the Wasmer reseller gateway from your website's server, never public browser code. "
        "Your API key uses your wallet balance, current custom rates, stock, and automatic delivery.\n\n"
        f"🌐 Base URL: <code>{escape(_base_url())}/api/v1</code>\n"
        "📚 Documentation: <a href=\"https://raindeals-api-staging.samarthp2727-rd.workers.dev/docs/\">Rain API Docs</a>\n\n"
        "Your active key stays visible here until you generate a new one or revoke it. "
        "Generating a new key immediately revokes the old key.",
        parse_mode="HTML",
        reply_markup=_api_menu(),
    )

@router.callback_query(F.data == "api_key_generate")
async def api_key_generate(callback: CallbackQuery):
    await callback.answer()
    try:
        raw_key, _ = await asyncio.to_thread(create_api_key, callback.from_user.id)
    except ApiKeyConfigurationError:
        await show(callback, "⚠️ <b>API Access Not Ready</b>\n\nThe administrator must add <code>API_KEY_PEPPER</code> to the server environment first.", parse_mode="HTML", reply_markup=_api_menu())
        return
    await show(
        callback,
        "✅ <b>NEW API KEY CREATED</b>\n\n"
        "You can open API Access anytime to view this active key again.\n\n"
        f"<code>{raw_key}</code>\n\n"
        "🔒 Keep it in your Wasmer server secret/environment variable. Do not place it in website JavaScript, HTML, screenshots, or GitHub.\n\n"
        f"API base: <code>{escape(_base_url())}/api/v1</code>",
        parse_mode="HTML", reply_markup=_api_menu(),
    )


@router.callback_query(F.data == "api_key_revoke")
async def api_key_revoke(callback: CallbackQuery):
    await callback.answer()
    revoked = await asyncio.to_thread(revoke_api_key, callback.from_user.id)
    text = "🛑 <b>API Key Revoked</b>\n\nThat key can no longer access your balance or products." if revoked else "ℹ️ <b>No Active API Key</b>\n\nThere was nothing to revoke."
    await show(callback, text, parse_mode="HTML", reply_markup=_api_menu())
