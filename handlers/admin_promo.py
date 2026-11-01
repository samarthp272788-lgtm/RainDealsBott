"""
handlers/admin_promo.py
"""

from utils.ui import answer_card

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
import random
import string

from database import SessionLocal
from models.promocode import PromoCode
import config

router = Router()


def is_admin(telegram_id: int) -> bool:
    return telegram_id in getattr(config, "ADMIN_IDS", [])


class PromoCodeCreation(StatesGroup):
    waiting_for_amount = State()
    waiting_for_max_uses = State()
    waiting_for_code = State()


def get_promo_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Create Promocode", callback_data="promo_create")],
        [InlineKeyboardButton(text="📋 List Promocodes", callback_data="promo_list")],
        [InlineKeyboardButton(text="❌ Delete Promocode", callback_data="promo_delete_menu")],
        [InlineKeyboardButton(text="⬅ Back", callback_data="admin_panel")],
    ])


@router.callback_query(F.data == "admin_promo")
async def promo_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "🎫 <b>Promocode Management</b>\n\nCreate, view, or delete promocodes.",
        reply_markup=get_promo_menu_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


# ── CREATE PROMOCODE ──

@router.callback_query(F.data == "promo_create")
async def promo_create_amount(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    await callback.message.edit_text(
        "💰 <b>Enter the USDT amount for this promocode:</b>\n\n"
        "Example: <code>10</code> or <code>5.5</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Cancel", callback_data="admin_promo")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(PromoCodeCreation.waiting_for_amount)
    await callback.answer()


@router.message(StateFilter(PromoCodeCreation.waiting_for_amount))
async def promo_amount_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.lower() == "cancel":
        await state.clear()
        await answer_card(message, "❌ Cancelled.", reply_markup=get_promo_menu_keyboard())
        return
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await answer_card(message, "❌ Invalid amount. Send a positive number:")
        return
    await state.update_data(amount=amount)
    await answer_card(message,
        f"🔢 <b>Max uses?</b>\n\nAmount: <b>{amount} USDT</b>\n<code>1</code> = single, <code>0</code> = unlimited",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅ Cancel", callback_data="admin_promo")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(PromoCodeCreation.waiting_for_max_uses)


@router.message(StateFilter(PromoCodeCreation.waiting_for_max_uses))
async def promo_max_uses_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.lower() == "cancel":
        await state.clear()
        await answer_card(message, "❌ Cancelled.", reply_markup=get_promo_menu_keyboard())
        return
    try:
        max_uses = int(text)
        if max_uses < 0:
            raise ValueError
    except ValueError:
        await answer_card(message, "❌ Invalid. Send 0 or a positive number:")
        return
    data = await state.get_data()
    await answer_card(message,
        f"🏷 <b>Send custom code or</b> <code>auto</code>:\n\n"
        f"Amount: <b>{data['amount']} USDT</b>\nMax: <b>{'Unlimited' if max_uses == 0 else max_uses}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Auto-Generate", callback_data="promo_auto_code")],
            [InlineKeyboardButton(text="⬅ Cancel", callback_data="admin_promo")]
        ]),
        parse_mode="HTML"
    )
    await state.update_data(max_uses=max_uses)
    await state.set_state(PromoCodeCreation.waiting_for_code)


@router.callback_query(F.data == "promo_auto_code", StateFilter(PromoCodeCreation.waiting_for_code))
async def promo_auto_code(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    data = await state.get_data()
    await _save_promocode(callback.message, state, data, code)
    await callback.answer()


@router.message(StateFilter(PromoCodeCreation.waiting_for_code))
async def promo_code_received(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip().upper()
    if text.lower() == "cancel":
        await state.clear()
        await answer_card(message, "❌ Cancelled.", reply_markup=get_promo_menu_keyboard())
        return
    data = await state.get_data()
    await _save_promocode(message, state, data, text)


async def _save_promocode(message: Message, state: FSMContext, data: dict, code: str):
    db = SessionLocal()
    try:
        existing = db.query(PromoCode).filter(PromoCode.code == code).first()
        if existing:
            await answer_card(message, "❌ Code already exists. Try another:")
            return
        promo = PromoCode(code=code, amount=data["amount"], max_uses=data["max_uses"], created_by=message.from_user.id)
        db.add(promo)
        db.commit()
        max_text = "Unlimited" if data["max_uses"] == 0 else str(data["max_uses"])
        await answer_card(message,
            f"✅ <b>Promocode Created!</b>\n\n🏷 <code>{code}</code>\n💰 <b>{data['amount']} USDT</b>\n🔢 Max Uses: <b>{max_text}</b>",
            reply_markup=get_promo_menu_keyboard(), parse_mode="HTML"
        )
    except Exception as e:
        db.rollback()
        await answer_card(message, f"❌ Error: {e}")
    finally:
        db.close()
        await state.clear()


# ── LIST PROMOCODES ──

@router.callback_query(F.data == "promo_list")
async def promo_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    db = SessionLocal()
    try:
        promos = db.query(PromoCode).order_by(PromoCode.created_at.desc()).limit(20).all()
        if not promos:
            await callback.message.edit_text("📋 <b>No promocodes found.</b>", reply_markup=get_promo_menu_keyboard(), parse_mode="HTML")
            await callback.answer()
            return
        text = "🎫 <b>Promocodes</b>\n\n"
        for p in promos:
            status = "✅" if p.is_active and p.can_use() else "❌"
            uses = f"{p.used_count}/{p.max_uses if p.max_uses > 0 else '∞'}"
            text += f"{status} <code>{p.code}</code> — <b>{p.amount} USDT</b> | Uses: {uses}\n\n"
        await callback.message.edit_text(text, reply_markup=get_promo_menu_keyboard(), parse_mode="HTML")
    finally:
        db.close()
    await callback.answer()


# ── DELETE PROMOCODE ──

@router.callback_query(F.data == "promo_delete_menu")
async def promo_delete_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    db = SessionLocal()
    try:
        promos = db.query(PromoCode).filter(PromoCode.is_active == True).order_by(PromoCode.created_at.desc()).limit(20).all()
        if not promos:
            await callback.message.edit_text("📋 <b>No active promocodes.</b>", reply_markup=get_promo_menu_keyboard(), parse_mode="HTML")
            await callback.answer()
            return
        buttons = [[InlineKeyboardButton(text=f"❌ {p.code} ({p.amount} USDT)", callback_data=f"promo_delete_{p.id}")] for p in promos]
        buttons.append([InlineKeyboardButton(text="⬅ Back", callback_data="admin_promo")])
        await callback.message.edit_text("❌ <b>Select a promocode to deactivate:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    finally:
        db.close()
    await callback.answer()


@router.callback_query(F.data.startswith("promo_delete_"))
async def promo_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Unauthorized", show_alert=True)
        return
    promo_id = int(callback.data.split("_")[-1])
    db = SessionLocal()
    try:
        promo = db.get(PromoCode, promo_id)
        if promo:
            promo.is_active = False
            db.commit()
            await callback.answer(f"✅ '{promo.code}' deactivated.", show_alert=True)
    finally:
        db.close()
    await promo_delete_menu(callback)