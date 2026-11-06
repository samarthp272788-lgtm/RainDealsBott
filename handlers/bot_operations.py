"""Aftercare and operational tools, using the existing single-card UI."""
import asyncio
from html import escape
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton as Button, InlineKeyboardMarkup
from config import ADMIN_IDS
from services import warranty
from services.input_forms import InputError, text_value, field_value, is_cancel
from utils.ui import show, answer_card, track_card
from services.screen_controller import managed_screen

router = Router()


class Inputs(StatesGroup):
    warranty_days = State()
    product_confirmation = State()
    issue_type = State()
    reason = State()
    deactivated = State()
    evidence = State()
    confirmation = State()
    resolution = State()
    metadata = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(order_id, admin=False):
    return keyboard([[Button(text='⬅ Back', callback_data=f'warranty_admin_{order_id}' if admin else f'warranty_order_{order_id}')]])


@router.callback_query(F.data.startswith('warranty_config_'))
async def configure_warranty(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    product_id = int(callback.data.rsplit('_', 1)[1])
    await callback.answer()
    await state.clear()
    await state.update_data(warranty_product=product_id)
    await show(callback, 'Choose aftercare for new purchases. Existing orders keep their saved policy.',
               reply_markup=keyboard([
                   [Button(text='🛡 Warranty / replacement', callback_data=f'warranty_policy_{product_id}_warranty')],
                   [Button(text='💸 Refund computation', callback_data=f'warranty_policy_{product_id}_refund')],
                   [Button(text='Skip · no claim button', callback_data=f'warranty_policy_{product_id}_skip')],
                   [Button(text='Cancel', callback_data=f'warranty_cancelconfig_{product_id}')]]))


@router.callback_query(F.data.startswith('warranty_policy_'))
async def choose_warranty_policy(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    _, _, product_id, mode = callback.data.split('_')
    product_id = int(product_id)
    if mode not in ('warranty', 'refund', 'skip'):
        await callback.answer('Invalid policy.', show_alert=True)
        return
    await callback.answer()
    await state.update_data(warranty_product=product_id, warranty_mode=mode)
    if mode == 'skip':
        error = await asyncio.to_thread(warranty.configure_policy, product_id, mode, 0, callback.from_user.id)
        from handlers.products import invalidate_products_cache
        invalidate_products_cache()
        await state.clear()
        await show(callback, error or '✅ Aftercare skipped for new purchases. No claim button.',
                   reply_markup=keyboard([[Button(text='Back to product', callback_data=f'manage_{product_id}')]]))
        return
    await state.set_state(Inputs.warranty_days)
    prompt = ('💸 Enter subscription length in days (1–3650).\n\n'+warranty.REFUND_RULES if mode == 'refund'
              else '🛡 Enter replacement warranty length in days (1–3650).')
    await show(callback, prompt+'\n\n/cancel to stop.', parse_mode='HTML',
               reply_markup=keyboard([[Button(text='Cancel', callback_data=f'warranty_cancelconfig_{product_id}')]]))


@router.callback_query(F.data.startswith('warranty_cancelconfig_'))
async def cancel_warranty_policy(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    from handlers.admin_product_manage import _refresh_manage_panel
    await _refresh_manage_panel(callback, int(callback.data.rsplit('_', 1)[1]))


@router.message(Inputs.warranty_days)
async def warranty_policy_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    if is_cancel(message.text):
        await state.clear()
        await answer_card(message, 'Cancelled.')
        return
    raw = (message.text or '').strip()
    if not raw.isascii() or not raw.isdigit() or len(raw) > 4 or not 1 <= int(raw) <= 3650:
        await answer_card(message, 'Send 1–3650 days, or /cancel.')
        return
    error = await asyncio.to_thread(warranty.configure_policy, data['warranty_product'], data['warranty_mode'], int(raw), message.from_user.id)
    if error:
        await answer_card(message, error)
        return
    from handlers.products import invalidate_products_cache
    invalidate_products_cache()
    await state.clear()
    await answer_card(message, '✅ Aftercare policy saved for new purchases.', reply_markup=keyboard([
        [Button(text='Back to product', callback_data=f"manage_{data['warranty_product']}")]]))


async def render_warranty(target, order_id, admin=False):
    ctx = await asyncio.to_thread(warranty.context, order_id, target.from_user.id, admin)
    if not ctx:
        await answer_card(target, 'Order not found.')
        return
    request = ctx['request']
    refund = ctx['policy'] == 'refund'
    text = warranty.summary_text(ctx)
    buttons = []
    buttons.append([Button(text='🧾 Order / product invoice',callback_data=f'warranty_invoice_{order_id}'),
                    Button(text='📦 Delivered information',callback_data=f'warranty_delivery_{order_id}')])
    buttons.append([Button(text='📋 Full order & submitted details',callback_data=f'order_{order_id}' if admin else f'order_detail_{order_id}')])
    if request:
        buttons.append([Button(text='💬 Conversation / more proof', callback_data=f'ct_thread_{order_id}')])
        evidence = request['evidence']
        buttons.append([Button(text='🧾 Request details', callback_data=f'warranty_details_{order_id}')])
        if evidence.get('kind') == 'photo' and evidence.get('file_id'):
            buttons.append([Button(text='📷 View evidence', callback_data=f'warranty_photo_{order_id}')])
        if admin and ctx['reviewable'] and request['status'] == 'submitted':
            if not refund or request['refund_amount'] is not None:
                label = f"Approve refund ${request['refund_amount']:.8f}" if refund else 'Approve replacement'
                buttons.append([Button(text=label, callback_data=f'warranty_approve_{order_id}', style='success')])
            buttons.append([Button(text='Decline', callback_data=f'warranty_reject_{order_id}', style='danger')])
        elif admin and ctx['reviewable'] and request['status'] == 'approved':
            buttons.append([Button(text='Deliver replacement', callback_data=f'warranty_complete_{order_id}', style='success')])
    elif ctx.get('can_submit',not ctx['error']):
        buttons.append([Button(text='💸 Ask for refund' if refund else '🛡 Claim warranty', callback_data=f'warranty_request_{order_id}', style='primary')])
    if admin and ctx.get('reviewable'):
        buttons.append([Button(text='🗓 Confirm subscription / delivery date',callback_data=f'warranty_metadata_{order_id}')])
    buttons.append([Button(text='Refresh', callback_data=f'warranty_admin_{order_id}' if admin else f'warranty_order_{order_id}'),
                    Button(text='⬅ Back', callback_data='warranty_queue_0' if admin else f'order_detail_{order_id}')])
    await answer_card(target, text, parse_mode='HTML', reply_markup=keyboard(buttons), replace_media=True)


@router.callback_query(F.data.startswith('warranty_details_'))
async def warranty_details(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.rsplit('_', 1)[1])
    admin = callback.from_user.id in ADMIN_IDS
    ctx = await asyncio.to_thread(warranty.context, order_id, callback.from_user.id, admin)
    if not ctx or not ctx['request']:
        await callback.answer('Request not found.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await show(callback, warranty.request_text(ctx), parse_mode='HTML', reply_markup=back(order_id, admin), replace_media=True)


@router.callback_query(F.data.startswith('warranty_order_'))
@router.callback_query(F.data.startswith('warranty_admin_'))
async def warranty_status(callback: CallbackQuery, state: FSMContext):
    admin = callback.data.startswith('warranty_admin_')
    if admin and callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await render_warranty(callback, int(callback.data.rsplit('_', 1)[1]), admin)


@router.callback_query(F.data.startswith('warranty_request_'))
async def request_warranty(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.rsplit('_', 1)[1])
    ctx = await asyncio.to_thread(warranty.context, order_id, callback.from_user.id)
    if not ctx or not ctx.get('can_submit',not ctx['error']) or ctx['request']:
        await callback.answer('Request unavailable. Refresh this order.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(Inputs.product_confirmation)
    await state.update_data(warranty_order=order_id,warranty_mode=ctx['policy'],warranty_product_confirmed=False)
    from services.claim_details import invoice_text
    await show(callback,invoice_text(ctx['invoice'])+'\n\nIs this the product with the issue?',parse_mode='HTML',
               reply_markup=keyboard([[Button(text='✅ Confirm product',callback_data=f'warranty_confirm_{order_id}',style='success')],
                                      [Button(text='Back',callback_data=f'warranty_order_{order_id}')]]))


@router.callback_query(F.data.startswith('warranty_confirm_'))
async def confirm_claim_product(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = int(callback.data.rsplit('_',1)[1])
    if await state.get_state() != Inputs.product_confirmation.state or data.get('warranty_order') != order_id:
        await callback.answer('Open the claim form from your order again.',show_alert=True)
        return
    await callback.answer()
    await state.update_data(warranty_product_confirmed=True)
    await state.set_state(Inputs.issue_type)
    from services.claim_details import ISSUES
    await show(callback,'What is wrong with this product?',reply_markup=keyboard([
        [Button(text=label,callback_data=f'warranty_issue_{order_id}_{code}')] for code,label in ISSUES.items()]))


@router.callback_query(F.data.startswith('warranty_issue_'))
async def choose_claim_issue(callback: CallbackQuery, state: FSMContext):
    from services.claim_details import ISSUES
    _,_,raw_order,issue = callback.data.split('_',3)
    order_id = int(raw_order)
    data = await state.get_data()
    if await state.get_state() != Inputs.issue_type.state or data.get('warranty_order') != order_id or not data.get('warranty_product_confirmed') or issue not in ISSUES:
        await callback.answer('Confirm the product first.',show_alert=True)
        return
    ctx = await asyncio.to_thread(warranty.context,order_id,callback.from_user.id)
    if not ctx or not ctx.get('can_submit',not ctx['error']) or ctx['request']:
        await callback.answer('This claim is no longer available.',show_alert=True)
        return
    await callback.answer()
    await state.update_data(warranty_issue_type=issue)
    await state.set_state(Inputs.reason)
    text = 'Describe what stopped working or was deactivated (10–1500 characters).\nDo not send passwords. /cancel to stop.'
    if ctx['policy'] == 'refund':
        text = warranty.REFUND_RULES+'\n\n'+warranty.quote_text(ctx['quote'])+'\n\n'+text
    await show(callback, text, parse_mode='HTML', reply_markup=back(order_id))


@router.message(Inputs.reason)
@router.message(Inputs.deactivated)
@router.message(Inputs.evidence)
async def warranty_input(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('warranty_order')
    if not order_id or is_cancel(message.text):
        await state.clear()
        if order_id:
            await render_warranty(message, order_id)
        else:
            await answer_card(message, 'Cancelled. Your order is unchanged.')
        return
    try:
        if await state.get_state() == Inputs.reason.state:
            reason = text_value(message.text, 1500, 10)
            await state.update_data(warranty_reason=reason)
            await state.set_state(Inputs.deactivated)
            await answer_card(message, 'When was it deactivated or last working? Include date, time and timezone if known.\nExample: 6 Nov, 2 PM IST. Send unknown if unsure.\n/cancel to stop.', reply_markup=back(order_id))
            return
        if await state.get_state() == Inputs.deactivated.state:
            await state.update_data(warranty_deactivated=text_value(message.text, 200))
            await state.set_state(Inputs.evidence)
            await answer_card(message, '📷 Upload a screenshot showing deactivation or the error. Hide passwords and unrelated personal details.\n'
                              + ('A screenshot is required for a refund.' if data['warranty_mode'] == 'refund' else 'You may also describe supporting evidence (10–2000 characters).')
                              + '\n/cancel to stop.', reply_markup=back(order_id))
            return
        if data['warranty_mode'] == 'refund' and not message.photo:
            raise InputError('Please upload a screenshot of the deactivation or error as a photo.')
        evidence = field_value(message, 'photo') if message.photo else {'kind': 'text', 'value': text_value(message.text, 2000, 10)}
    except InputError as error:
        await answer_card(message, str(error), reply_markup=back(order_id))
        return
    await state.update_data(warranty_evidence=evidence)
    await state.set_state(Inputs.confirmation)
    await claim_confirmation(message, state)


@router.message(Inputs.product_confirmation)
@router.message(Inputs.issue_type)
async def claim_choice_input(message: Message,state: FSMContext):
    data = await state.get_data()
    order_id = data.get('warranty_order')
    if is_cancel(message.text):
        await state.clear()
        await render_warranty(message,order_id)
        return
    if await state.get_state() == Inputs.product_confirmation.state:
        rows = [[Button(text='✅ Confirm product',callback_data=f'warranty_confirm_{order_id}')]]
    else:
        from services.claim_details import ISSUES
        rows = [[Button(text=label,callback_data=f'warranty_issue_{order_id}_{code}')] for code,label in ISSUES.items()]
    rows.append([Button(text='Cancel',callback_data=f'warranty_order_{order_id}')])
    await answer_card(message,'Choose an option below, or /cancel.',reply_markup=keyboard(rows))


async def claim_confirmation(target, state, notice=''):
    data = await state.get_data()
    order_id = data['warranty_order']
    ctx = await asyncio.to_thread(warranty.context, order_id, target.from_user.id)
    if not ctx or not ctx.get('can_submit',not ctx['error']) or ctx['request']:
        await state.clear()
        await render_warranty(target, order_id)
        return
    refund = ctx['policy'] == 'refund'
    await state.update_data(warranty_expected_refund=str(ctx['quote']['refund_amount']) if refund and ctx['quote'] else None)
    text = (escape(notice)+'\n\n' if notice else '') + f'🧾 <b>Review claim · Order #{order_id}</b>\n\n'
    text += 'Problem: '+escape(data['warranty_reason'][:600])+'\nDeactivated (reported): '+escape(data['warranty_deactivated'])
    text += '\nProof: screenshot attached.' if data['warranty_evidence']['kind'] == 'photo' else '\nProof: description attached.'
    if refund:
        text += '\n\n'+warranty.quote_text(ctx['quote'])
    text += '\n\nSubmit for admin review? No credit or replacement is issued before approval.'
    await answer_card(target, text, parse_mode='HTML', reply_markup=keyboard([
        [Button(text='Submit claim', callback_data=f'warranty_submit_{order_id}', style='success')],
        [Button(text='Cancel', callback_data=f'warranty_order_{order_id}')]]), replace_media=True)


@router.callback_query(F.data.startswith('warranty_submit_'))
async def submit_claim(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = int(callback.data.rsplit('_', 1)[1])
    if await state.get_state() != Inputs.confirmation.state or data.get('warranty_order') != order_id:
        await callback.answer('This form has expired. Open your order again.', show_alert=True)
        return
    await callback.answer()
    if not data.get('warranty_product_confirmed'):
        await answer_card(callback,'Confirm the purchased product first.',reply_markup=back(order_id))
        return
    proof = None
    if data['warranty_evidence']['kind'] == 'photo':
        from io import BytesIO
        from services.claim_details import proof_mime
        try:
            buffer = BytesIO()
            async with asyncio.timeout(20):
                await callback.bot.download(data['warranty_evidence']['file_id'],destination=buffer)
            proof = buffer.getvalue()
            proof_mime(proof)
        except Exception:
            await answer_card(callback,'Could not save the screenshot for staff review. Try Submit claim again; your form is still saved.',reply_markup=keyboard([
                [Button(text='Retry submit',callback_data=f'warranty_submit_{order_id}')],[Button(text='Back',callback_data=f'warranty_order_{order_id}')]]))
            return
    error = await asyncio.to_thread(warranty.submit, order_id, callback.from_user.id,
        data['warranty_reason'], data['warranty_evidence'], data['warranty_deactivated'], data.get('warranty_expected_refund'),
        issue_type=data.get('warranty_issue_type','other'),product_confirmed=True,proof_bytes=proof)
    if error:
        await claim_confirmation(callback, state, error)
        return
    await state.clear()
    await render_warranty(callback, order_id)


@router.message(Inputs.confirmation)
async def claim_confirmation_input(message: Message, state: FSMContext):
    if is_cancel(message.text):
        data = await state.get_data()
        await state.clear()
        await render_warranty(message, data['warranty_order'])
        return
    await claim_confirmation(message, state, 'Use Submit claim, or /cancel.')


@router.callback_query(F.data.startswith('warranty_queue_'))
async def warranty_queue(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    page = max(0, int(callback.data.rsplit('_', 1)[1]))
    total, rows = await asyncio.to_thread(warranty.pending, page)
    buttons = [[Button(text=f"#{r['order_id']} · {r['request_type']} · {r['status']}", callback_data=f"warranty_admin_{r['order_id']}")] for r in rows]
    nav = []
    if page:
        nav.append(Button(text='⬅ Previous', callback_data=f'warranty_queue_{page-1}'))
    if (page+1)*8 < total:
        nav.append(Button(text='Next ➡', callback_data=f'warranty_queue_{page+1}'))
    if nav:
        buttons.append(nav)
    buttons.append([Button(text='⬅ Admin', callback_data='admin_panel')])
    await show(callback, f'🛡 <b>Warranty / refund requests</b>\n{total} awaiting review/delivery · Page {page+1}',
               parse_mode='HTML', reply_markup=keyboard(buttons), replace_media=True)


@router.callback_query(F.data.regexp(r'^warranty_(approve|reject|complete)_\d+$'))
async def warranty_action(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    _, operation, order_id = callback.data.split('_')
    order_id = int(order_id)
    if operation == 'approve':
        ctx = await asyncio.to_thread(warranty.context, order_id, callback.from_user.id, True)
        if ctx and ctx['request'] and ctx['request']['request_type'] == 'refund':
            await callback.answer()
            await show(callback, 'Confirm you checked the deactivation screenshot and reported dates.\n\n'
                       + warranty.summary_text(ctx)+'\n\nApproval credits the saved amount to the buyer wallet once.', parse_mode='HTML',
                       reply_markup=keyboard([[Button(text='Proof checked · Approve refund', callback_data=f'warranty_refundapprove_{order_id}', style='success')],
                                              [Button(text='Back', callback_data=f'warranty_admin_{order_id}')]]), replace_media=True)
            return
        error = await asyncio.to_thread(warranty.review, order_id, callback.from_user.id, operation)
        await callback.answer(error or 'Approved.', show_alert=bool(error))
        await render_warranty(callback, order_id, True)
        return
    await callback.answer()
    await state.set_state(Inputs.resolution)
    await state.update_data(warranty_order=order_id, warranty_operation=operation)
    prompt = 'Send replacement details (up to 3000 characters).' if operation == 'complete' else 'Send the reason for declining this request.'
    await show(callback, prompt+'\n/cancel to stop.', reply_markup=back(order_id, True))


@router.callback_query(F.data.startswith('warranty_refundapprove_'))
async def approve_refund(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    order_id = int(callback.data.rsplit('_', 1)[1])
    ctx = await asyncio.to_thread(warranty.context, order_id, callback.from_user.id, True)
    if not ctx or not ctx['request'] or ctx['request']['request_type'] != 'refund':
        await callback.answer('Refund claim not found.', show_alert=True)
        return
    error = await asyncio.to_thread(warranty.review, order_id, callback.from_user.id, 'approve')
    await callback.answer(error or 'Refund credited.', show_alert=bool(error))
    await state.clear()
    await render_warranty(callback, order_id, True)


@router.message(Inputs.resolution)
async def warranty_resolution(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    if is_cancel(message.text):
        await state.clear()
        await render_warranty(message, data['warranty_order'], True)
        return
    error = await asyncio.to_thread(warranty.review, data['warranty_order'], message.from_user.id,
                                    data['warranty_operation'], message.text)
    if error:
        await answer_card(message, error, reply_markup=back(data['warranty_order'], True))
        return
    await state.clear()
    await render_warranty(message, data['warranty_order'], True)


@router.callback_query(F.data.startswith('warranty_photo_'))
async def warranty_photo(callback: CallbackQuery):
    order_id = int(callback.data.rsplit('_', 1)[1])
    admin = callback.from_user.id in ADMIN_IDS
    ctx = await asyncio.to_thread(warranty.context, order_id, callback.from_user.id, admin)
    request = ctx.get('request') if ctx else None
    if not request or request['evidence'].get('kind') != 'photo':
        await callback.answer('Evidence not found.', show_alert=True)
        return
    await callback.answer()
    if not request.get('proof_available'):
        try:
            from io import BytesIO
            image = BytesIO()
            async with asyncio.timeout(15):
                await callback.bot.download(request['evidence']['file_id'],destination=image)
            await asyncio.to_thread(warranty.cache_proof,order_id,callback.from_user.id,image.getvalue(),admin)
        except Exception:
            pass  # Telegram evidence still opens; web shows a clear missing-cache notice.
    await show_warranty_photo(callback, request['evidence']['file_id'], order_id, admin)


@router.callback_query(F.data.startswith('warranty_invoice_'))
async def claim_invoice(callback: CallbackQuery):
    admin = callback.from_user.id in ADMIN_IDS
    order_id = int(callback.data.rsplit('_',1)[1])
    ctx = await asyncio.to_thread(warranty.context,order_id,callback.from_user.id,admin)
    if not ctx:
        await callback.answer('Order not found.',show_alert=True)
        return
    from services.claim_details import invoice_text
    await callback.answer()
    await show(callback,invoice_text(ctx['invoice']),parse_mode='HTML',reply_markup=back(order_id,admin),replace_media=True)


@router.callback_query(F.data.startswith('warranty_delivery_'))
async def claim_delivery(callback: CallbackQuery):
    admin = callback.from_user.id in ADMIN_IDS
    order_id = int(callback.data.rsplit('_',1)[1])
    value = await asyncio.to_thread(warranty.delivery_details,order_id,callback.from_user.id,admin)
    if value is None:
        await callback.answer('Order not found.',show_alert=True)
        return
    await callback.answer()
    await show(callback,f'📦 <b>Order #{order_id} · Delivery saved for this claim</b>\n\n<code>'+escape(value[:3000])+'</code>',
               parse_mode='HTML',reply_markup=back(order_id,admin),replace_media=True)


@router.callback_query(F.data.startswith('warranty_metadata_'))
async def claim_metadata(callback: CallbackQuery,state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.',show_alert=True)
        return
    order_id = int(callback.data.rsplit('_',1)[1])
    await callback.answer()
    await state.set_state(Inputs.metadata)
    await state.update_data(warranty_order=order_id)
    await show(callback,'Confirm the real subscription length and delivery timestamp. Do not use today for an older delivery.\n\n'
               'Send: days | YYYY-MM-DD HH:MM (UTC) | reason\nExample: 30 | 2026-11-02 10:30 | Checked delivery history\n/cancel to stop.',reply_markup=back(order_id,True))


@router.message(Inputs.metadata)
async def claim_metadata_input(message: Message,state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    if is_cancel(message.text):
        await state.clear()
        await render_warranty(message,data['warranty_order'],True)
        return
    parts = (message.text or '').split('|',2)
    if len(parts)!=3 or not parts[0].strip().isdigit():
        await answer_card(message,'Send: days | YYYY-MM-DD HH:MM (UTC) | reason',reply_markup=back(data['warranty_order'],True))
        return
    error = await asyncio.to_thread(warranty.correct_metadata,data['warranty_order'],message.from_user.id,
                                    int(parts[0]),parts[1].strip(),parts[2].strip())
    if error:
        await answer_card(message,error,reply_markup=back(data['warranty_order'],True))
        return
    await state.clear()
    await render_warranty(message,data['warranty_order'],True)


@managed_screen
async def show_warranty_photo(callback, file_id, order_id, admin):
    photo = await callback.bot.send_photo(callback.message.chat.id, file_id,
                                          caption=f'Evidence for order #{order_id}', reply_markup=back(order_id, admin))
    track_card(callback.from_user.id, photo.chat.id, photo.message_id)
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith('ops_'))
async def operations(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    buttons = []
    if callback.data == 'ops_performance' or callback.data.startswith('ops_providers_'):
        text = 'This tool is not available in the bot.'
    elif callback.data.startswith('ops_resolveconfirm_'):
        job_id = int(callback.data.rsplit('_', 1)[1])
        text = 'Check the recipient’s chat first. This closes the uncertain send without resending it or claiming confirmed Telegram delivery.'
        buttons.append([Button(text='Resolve without resending', callback_data=f'ops_resolve_{job_id}')])
        buttons.append([Button(text='Cancel', callback_data='ops_notifications_0')])
    elif callback.data.startswith('ops_resolve_'):
        from services.notification_outbox import resolve_uncertain
        error = await asyncio.to_thread(resolve_uncertain, int(callback.data.rsplit('_', 1)[1]), callback.bot.id, callback.from_user.id)
        text = error or 'Resolved without sending another message.'
        buttons.append([Button(text='Notifications', callback_data='ops_notifications_0')])
    elif callback.data.startswith('ops_retry_'):
        from services.notification_outbox import retry_failed
        error = await asyncio.to_thread(retry_failed, int(callback.data.rsplit('_', 1)[1]), callback.bot.id, callback.from_user.id)
        text = error or '✅ Notification queued again.'
        buttons.append([Button(text='Notifications', callback_data='ops_notifications_0')])
    elif callback.data.startswith('ops_notifications_'):
        from services.notification_outbox import report
        page = max(0, int(callback.data.rsplit('_', 1)[1]))
        counts, rows = await asyncio.to_thread(report, callback.bot.id, page)
        text = '🔔 <b>Notification queue</b>\n' + ' · '.join(f'{escape(k)}: {v}' for k,v in counts.items())
        text += '\n\nUncertain sends need review; automatic retry is disabled.\n'
        for row in rows:
            text += f"\n#{row['id']} · {row['status']} · recipient {escape(str(row['chat_id']))}\nAttempts: {row['attempts']} · {escape(row['error'] or 'No error')}\n"
            if row['status'] == 'failed' and row['attempts'] < 10:
                buttons.append([Button(text=f"Retry failed #{row['id']}", callback_data=f"ops_retry_{row['id']}")])
            elif row['status'] == 'uncertain':
                buttons.append([Button(text=f"Review uncertain #{row['id']}", callback_data=f"ops_resolveconfirm_{row['id']}")])
        nav = [Button(text='Refresh', callback_data=f'ops_notifications_{page}')]
        if page:
            nav.append(Button(text='Previous', callback_data=f'ops_notifications_{page-1}'))
        if (page+1)*8 < sum(counts.values()):
            nav.append(Button(text='Next', callback_data=f'ops_notifications_{page+1}'))
        buttons.append(nav)
    else:
        text = 'Option not found.'
    buttons.append([Button(text='⬅ Admin', callback_data='admin_panel')])
    await show(callback, text, parse_mode='HTML', reply_markup=keyboard(buttons), replace_media=True)
