"""Resume display/input steps only; never replay payment or purchase actions."""
import asyncio
from datetime import datetime
import logging
import time
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from services.session_resume import load_resume, make_snapshot, save_resume, saved_prompt, restore_data, is_admin, admin_state
from services.checkout_actions import confirmation_token
from utils.ui import answer_card, begin_resumed_card, finish_resumed_card, get_card_location

router = Router()
logger = logging.getLogger(__name__)
_running_restarts = {}


async def stop_restart(key):
    task = _running_restarts.get(key)
    if task and task is not asyncio.current_task():
        _running_restarts.pop(key, None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ResumeCallback(CallbackQuery):
    resume_wipe_in: bool = True

    async def answer(self, *args, **kwargs):
        # /restart is a message, so there is no Telegram callback ID to answer.
        return True


@router.message(Command('restart'))
async def restart_cmd(message: Message, state: FSMContext):
    if message.chat.type != 'private':
        return
    key = (message.from_user.id, message.chat.id)
    await stop_restart(key)
    task = asyncio.create_task(_restart_saved_screen(message, state))
    _running_restarts[key] = task
    try:
        await task
    except asyncio.CancelledError:
        if _running_restarts.get(key) is task:
            raise
    finally:
        if _running_restarts.get(key) is task:
            _running_restarts.pop(key, None)


async def _restart_saved_screen(message: Message, state: FSMContext):
    key = (message.from_user.id, message.chat.id)
    try:
        allowed_admin = is_admin(key[0])
        live_state = await state.get_state()
        if admin_state(live_state) and not allowed_admin:
            await state.clear()
            save_resume(key,None)
            await answer_card(message,'Admin access is no longer available. Send /start.')
            return
        # Use current input first: a quick /restart may precede the deferred DB save.
        snapshot = make_snapshot(live_state,await state.get_data(),admin=allowed_admin,prompt=saved_prompt(key)) if live_state else None
        if not snapshot:
            snapshot = await load_resume(key)
    except Exception:
        logger.exception('Could not load resume session for %s', key[0])
        await answer_card(message, 'Could not load your last step. Try /restart again or /start for Home.')
        return
    if not snapshot:
        from handlers.start import start_cmd
        await start_cmd(message, None, state)
        return
    # Revalidate persisted state against the current allowlist.
    if snapshot.get('admin') and not allowed_admin:
        await state.clear()
        save_resume(key,None)
        await answer_card(message,'Admin access is no longer available. Send /start.')
        return
    snapshot = make_snapshot(snapshot.get('state'), snapshot.get('data', {}), snapshot.get('navigation'),
                             admin=allowed_admin,prompt=snapshot.get('prompt'))
    if not snapshot:
        await answer_card(message, 'This step cannot be resumed. Send /start.')
        return
    try:
        await _restore_snapshot(message, state, snapshot)
    except Exception:
        logger.exception('Could not render resumed session for %s', key[0])
        await answer_card(message, 'Could not resume this step. Try /restart again or /start for Home.', state=state)


async def _restore_snapshot(message, state, snapshot):
    key = (message.from_user.id, message.chat.id)
    restored_data = restore_data(snapshot['data'],message.bot)
    await state.set_data(restored_data)
    await state.set_state(snapshot['state'])
    card = await begin_resumed_card(message, state, restored_data)
    callback = ResumeCallback(id='restart', from_user=message.from_user, chat_instance='restart',
                              message=card, data=snapshot.get('navigation') or '').as_(message.bot)
    saved_state = snapshot['state'] or ''
    if saved_state == 'DepositState:waiting_txid':
        from handlers.deposit import _load_invoice_for_resume, resume_deposit, NETWORK_CALLBACKS
        invoice = await asyncio.to_thread(_load_invoice_for_resume, key[0], snapshot['data'].get('deposit_id'))
        if not invoice or (not invoice.get('tx_hash') and (not invoice.get('expires_at') or datetime.fromisoformat(invoice['expires_at']) <= datetime.utcnow())):
            await state.clear()
            save_resume(key, None)
            network = snapshot['data'].get('network')
            renew = next((name for name, value in NETWORK_CALLBACKS.items() if value == network), 'deposit_start')
            await answer_card(message, 'This invoice is closed or expired. Your wallet and recorded payments are unchanged.',
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                  InlineKeyboardButton(text='Open payment step', callback_data=renew),
                                  InlineKeyboardButton(text='Home', callback_data='main_menu')]]))
            return
        callback = callback.model_copy(update={'data': f"resume_deposit_{invoice['id']}"}).as_(message.bot)
        try:
            await resume_deposit(callback, state)
        finally:
            await finish_resumed_card(message, state, card)
    elif saved_state == 'PurchaseStates:waiting_qty':
        product_id = snapshot['data'].get('pending_product_id')
        quantity = snapshot['data'].get(f'qty_{product_id}')
        if quantity:
            from handlers.products import _fetch_product, _get_custom_price, _get_max_qty, _real_stock, _qty_text, _confirm_keyboard
            product, price = await asyncio.gather(asyncio.to_thread(_fetch_product, product_id),
                                                  asyncio.to_thread(_get_custom_price, key[0], product_id))
            if product and product.is_active and 0 < int(quantity) <= _get_max_qty(product):
                token = await confirmation_token(state, product_id, reuse=True)
                await answer_card(message, _qty_text(product, int(quantity), _real_stock(product) > 0, price), state=state,
                                  parse_mode='HTML', reply_markup=_confirm_keyboard(product_id, float(product.price) == 0, checkout_token=token))
            else:
                await answer_card(message, 'Stock changed. Send a new quantity or /start for Home.', state=state)
        else:
            await answer_card(message, 'Continue your order. Send the quantity. Stock and price are checked again before purchase.', state=state)
    elif saved_state == 'OrderRequirementStates:waiting_input':
        from handlers.orders import _order_requirement_context, _requirement_prompt
        order_id = snapshot['data'].get('order_requirement_order_id')
        order, requirements, submission = await asyncio.to_thread(_order_requirement_context, order_id, key[0])
        index = int(snapshot['data'].get('order_requirement_index', 0))
        if not order or order.status != 'pending_info' or submission or not 0 <= index < len(requirements):
            await state.clear()
            save_resume(key, None)
            await answer_card(message, 'This information step is closed. Check My Orders or send /start.')
            return
        await state.update_data(order_requirement_fields=requirements)
        await answer_card(message, _requirement_prompt(requirements[index], index, len(requirements)), state=state, parse_mode='HTML')
    elif snapshot.get('prompt') and (saved_state or not snapshot.get('navigation')):
        prompt = snapshot['prompt']
        markup = InlineKeyboardMarkup.model_validate(prompt['reply_markup']) if prompt.get('reply_markup') else None
        if prompt.get('photo'):
            from utils.ui import track_card
            shown = await message.bot.send_photo(message.chat.id,prompt['photo'],caption=prompt['text'],
                                                  parse_mode=prompt.get('parse_mode','HTML'),reply_markup=markup)
            track_card(key[0],shown.chat.id,shown.message_id)
            await state.update_data(_card_chat_id=shown.chat.id,_card_message_id=shown.message_id)
            await finish_resumed_card(message,state,card)
        else:
            await answer_card(message,prompt['text'],state=state,parse_mode=prompt.get('parse_mode','HTML'),reply_markup=markup)
    elif saved_state:
        prompts = {'SearchStates:waiting_query': 'Continue searching. Send a product name.',
                   'DepositState:waiting_promo_code': 'Continue. Send your promo code.',
                   'DepositState:waiting_optional_promo': 'Continue. Send your promo code.',
                   'DepositState:waiting_amount': 'Continue. Send the deposit amount.',
                   'SupportState:waiting_message': 'Continue your ticket. Send your message.',
                   'SupportState:tracking_ticket': 'Continue tracking. Send your ticket ID.',
                   'OrderFilterStates:waiting_filter': 'Continue searching. Send your order ID.'}
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text='Payment methods', callback_data='deposit_start'),
                              InlineKeyboardButton(text='Home', callback_data='main_menu')]]) if saved_state.startswith('DepositState:') else None
        fallback = 'Continue this step. Send '+saved_state.rsplit(':',1)[-1].replace('_',' ')+'. /start opens Home.'
        await answer_card(message, prompts.get(saved_state, fallback), state=state, reply_markup=keyboard)
    else:
        from handlers.products import (
            products_menu, products_all, products_category, product_info,
            freebies_menu, freebies_all, freebies_category,
        )
        from handlers.orders import my_orders, order_detail
        from handlers.deposit import deposit_menu
        nav = snapshot.get('navigation') or ''
        if nav == 'products_menu':
            await products_menu(callback)
        elif nav == 'products_all':
            await products_all(callback)
        elif nav.startswith('products_cat_'):
            await products_category(callback)
        elif nav == 'freebies_menu':
            await freebies_menu(callback)
        elif nav == 'freebies_all':
            await freebies_all(callback)
        elif nav.startswith('freebies_cat_'):
            await freebies_category(callback)
        elif nav.startswith('product_'):
            await product_info(callback)
        elif nav == 'orders_menu':
            await my_orders(callback)
        elif nav.startswith('order_detail_'):
            await order_detail(callback)
        elif nav == 'deposit_start':
            await deposit_menu(callback, state)
        else:
            await answer_card(message, 'Send /start to open Home.')
    location = get_card_location(*key)
    if location:
        await state.update_data(_card_chat_id=location[0], _card_message_id=location[1])
    save_resume(key, make_snapshot(await state.get_state(), await state.get_data(), snapshot.get('navigation'),
                                  admin=is_admin(key[0]),prompt=saved_prompt(key),last_activity=time.time()))


@router.callback_query(F.data.in_({'session_resume', 'session_home'}))
async def session_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    message = callback.message.model_copy(update={'from_user': callback.from_user}).as_(callback.bot)
    if callback.data == 'session_resume':
        await restart_cmd(message, state)
    else:
        from handlers.start import start_cmd
        await start_cmd(message, None, state)
