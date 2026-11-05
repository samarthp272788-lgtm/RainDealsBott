"""Order history for customers and a private team fulfillment queue."""
import asyncio
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Button, InlineKeyboardMarkup, Message

from config import ADMIN_IDS
from services.order_workflow import read_timeline, timeline_text, queue_orders, work_details, change_work
from utils.ui import answer_card, show

router = Router()
VIEWS = {'all': 'All active', 'ready': 'Ready', 'info': 'Waiting for info',
         'mine': 'Mine', 'unassigned': 'Unassigned'}


class WorkInput(StatesGroup):
    assignee = State()
    note = State()


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith('order_timeline_'))
@router.callback_query(F.data.startswith('admin_timeline_'))
async def order_timeline(callback: CallbackQuery):
    admin = callback.data.startswith('admin_')
    if admin and callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    order_id = int(callback.data.rsplit('_', 1)[1])
    await callback.answer()
    timeline = await asyncio.to_thread(read_timeline, order_id, callback.from_user.id, admin)
    if not timeline:
        await show(callback, 'Order not found.')
        return
    await show(callback, timeline_text(timeline, admin), parse_mode='HTML', reply_markup=markup([
        [Button(text='⬅ Back to order', callback_data=f'order_{order_id}_0' if admin else f'order_detail_{order_id}')]]))


@router.callback_query(F.data.startswith('work_queue_'))
async def fulfillment_queue(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    _, _, view, page = callback.data.split('_')
    view = view if view in VIEWS else 'all'
    await callback.answer()
    await state.clear()
    rows, page, total = await asyncio.to_thread(queue_orders, view, int(page), callback.from_user.id)
    buttons = [[Button(text=('• ' if key == view else '') + label, callback_data=f'work_queue_{key}_0')
                for key, label in list(VIEWS.items())[i:i+2]] for i in range(0, len(VIEWS), 2)]
    for order in rows:
        assigned = str(order['assigned']) if order['assigned'] else 'Unassigned'
        buttons.append([Button(text=f"{'🔴 ' if order['priority'] == 'high' else ''}#{order['id']} · {order['name'][:30]} · {order['remaining']} left · {assigned}",
                               callback_data=f"work_view_{order['id']}")])
    nav = []
    if page:
        nav.append(Button(text='⬅ Previous', callback_data=f'work_queue_{view}_{page-1}'))
    if (page+1)*8 < total:
        nav.append(Button(text='Next ➡', callback_data=f'work_queue_{view}_{page+1}'))
    if nav:
        buttons.append(nav)
    buttons.append([Button(text='📦 All orders', callback_data='admin_orders')])
    await show(callback, f'📋 <b>Fulfillment queue · {VIEWS[view]}</b>\n\n{total} orders · Page {page+1}\n'
               'High priority first, then oldest.\n' + ('Select an order to manage it.' if rows else 'No orders in this view.'),
               parse_mode='HTML', reply_markup=markup(buttons))


async def render_work(target, order_id):
    order = await asyncio.to_thread(work_details, order_id)
    if not order:
        await answer_card(target, 'Order not found.')
        return
    text = (f"📋 <b>Order #{order_id}</b>\n{escape(order['name'])}\n\n"
            f"Status: {escape(order['status'])}\nAutomatic: {order['automatic']} · Remaining: {order['remaining']}\n"
            f"Assigned: {order['assigned'] or 'Unassigned'}\nPriority: {order['priority']}\n\n<b>Private team notes</b>\n")
    for note in reversed(order['notes']):
        at = note['at'].strftime('%d %b %H:%M UTC')
        text += f"{at} · {note['admin']}\n{escape(note['body'])}\n\n"
    if not order['notes']:
        text += 'No notes yet.\n'
    buttons = [[Button(text='View order', callback_data=f'order_{order_id}_0'),
                Button(text='Timeline', callback_data=f'admin_timeline_{order_id}')]]
    if order['active']:
        buttons += [[Button(text='Take order', callback_data=f'work_claim_{order_id}'),
                     Button(text='Release', callback_data=f'work_release_{order_id}')],
                    [Button(text='Assign admin', callback_data=f'work_assign_{order_id}'),
                     Button(text='Toggle priority', callback_data=f'work_priority_{order_id}')]]
    buttons += [[Button(text='Add private note', callback_data=f'work_note_{order_id}')],
                [Button(text='⬅ Fulfillment queue', callback_data='work_queue_all_0')]]
    await answer_card(target, text, parse_mode='HTML', reply_markup=markup(buttons))


@router.callback_query(F.data.startswith('work_view_'))
async def view_work(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await render_work(callback, int(callback.data.rsplit('_', 1)[1]))


@router.callback_query(F.data.regexp(r'^work_(claim|release|priority|assign|note)_\d+$'))
async def work_action(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer('Access denied.', show_alert=True)
        return
    _, operation, order_id = callback.data.split('_')
    order_id = int(order_id)
    if operation in ('assign', 'note'):
        await callback.answer()
        await state.set_state(WorkInput.assignee if operation == 'assign' else WorkInput.note)
        await state.update_data(work_order_id=order_id)
        prompt = ('Send the admin Telegram ID.\nConfigured admins: ' + ', '.join(map(str, ADMIN_IDS))
                  if operation == 'assign' else 'Send a private team note (up to 1000 characters). Do not include passwords.')
        await show(callback, prompt, reply_markup=markup([[Button(text='Cancel', callback_data=f'work_view_{order_id}')]]))
        return
    error = await asyncio.to_thread(change_work, order_id, callback.from_user.id, operation)
    await callback.answer(error or 'Saved.', show_alert=bool(error))
    await render_work(callback, order_id)


@router.message(WorkInput.assignee)
@router.message(WorkInput.note)
async def work_input(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or '').startswith('/'):
        await state.clear()
        await answer_card(message, 'Cancelled.', reply_markup=markup([[Button(text='Fulfillment queue', callback_data='work_queue_all_0')]]))
        return
    current = await state.get_state()
    data = await state.get_data()
    operation = 'assign' if current == WorkInput.assignee.state else 'note'
    value = message.text or ''
    if operation == 'assign':
        if not value.strip().isdigit():
            await answer_card(message, 'Send a numeric admin Telegram ID.')
            return
        value = int(value.strip())
    order_id = data.get('work_order_id')
    if not order_id:
        await state.clear()
        return
    error = await asyncio.to_thread(change_work, order_id, message.from_user.id, operation, value)
    if error:
        await answer_card(message, error)
        return
    await state.clear()
    await render_work(message, order_id)
