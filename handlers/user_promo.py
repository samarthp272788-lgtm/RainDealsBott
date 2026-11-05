"""
handlers/user_promo.py
"""

import asyncio
from html import escape
from services.promotions import redeem
from services.input_forms import is_cancel
import logging

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from utils.ui import show, update_card

router = Router()
logger = logging.getLogger(__name__)


class RedeemState(StatesGroup):
    waiting_for_code = State()


@router.message(Command("redeem"))
async def redeem_command(message: Message, state: FSMContext):
    parts = (message.text or "").split()

    if len(parts) >= 2:
        await _process_redeem(message, parts[1].strip().upper(), state=state)
        return

    await update_card(
        message, state,
        "🎫 <b>Redeem a Promocode</b>\n\n"
        "Send the promocode or type <code>cancel</code>.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅ Cancel",
                        callback_data="deposit_start",
                    )
                ]
            ]
        ),
        parse_mode="HTML",
    )

    await state.set_state(RedeemState.waiting_for_code)


@router.callback_query(F.data == "redeem_promo")
async def redeem_button(callback: CallbackQuery, state: FSMContext):
    await show(
        callback,
        "🎫 <b>Redeem a Promocode</b>\n\n"
        "Please send your promocode:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅ Cancel",
                        callback_data="deposit_start",
                    )
                ]
            ]
        ),
        parse_mode="HTML",
        state=state,
    )

    await state.set_state(RedeemState.waiting_for_code)
    await callback.answer()


@router.message(RedeemState.waiting_for_code)
async def redeem_code_input(message: Message, state: FSMContext):
    text = (message.text or "").strip().upper()

    if is_cancel(message.text):
        await update_card(message, state, "❌ Redeem cancelled.")
        await state.clear()
        return

    await _process_redeem(message, text, state=state)
    await state.clear()


async def _process_redeem(message: Message, code: str, state: FSMContext | None = None):
    try:
        success, result = await asyncio.to_thread(redeem, message.from_user.id, code)
    except Exception:
        logger.exception("Promocode redemption could not be completed")
        await update_card(message, state, 'Could not redeem the promocode. Check your wallet before retrying.')
        return
    if not success:
        await update_card(message, state, result)
        return
    await update_card(message, state,
        '🎉 <b>Promocode Redeemed!</b>\n\n'
        f"🏷 <code>{escape(result['code'])}</code>\n"
        f"💰 <b>+{result['amount']} USDT</b>\n"
        f"💳 Balance: <b>{result['balance']} USDT</b>",
        parse_mode='HTML', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text='⬅ Main Menu', callback_data='main_menu')]]))
