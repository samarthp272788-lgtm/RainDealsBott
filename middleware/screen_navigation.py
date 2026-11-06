from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from services.screen_controller import current_screen, get_screen, GuardedState, SupersededScreen


def is_navigation(event):
    if isinstance(event, Message):
        return True
    value = event.data or ''
    exact = {'products_menu', 'products_all', 'main_menu', 'orders_menu', 'my_profile',
             'favorites_menu', 'restock_alerts_menu', 'deposit_start', 'my_deposits',
             'support_menu', 'admin_panel', 'admin_orders', 'session_home', 'session_resume',
             'deposit_bep20', 'deposit_polygon', 'deposit_binance', 'deposit_upi', 'deposit_cancel', 'deposit_promo'}
    return value in exact or value.startswith((
        'products_cat_', 'product_', 'select_qty_', 'order_detail_', 'orders_page_',
        'order_timeline_', 'admin_timeline_', 'work_queue_', 'work_view_', 'admin_orders_page_',
        'resume_deposit_', 'cancel_buy_', 'order_', 'admin_', 'warranty_', 'addcare_', 'ops_', 'ct_'))


class ScreenNavigationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not message or not event.from_user:
            return await handler(event, data)
        key = (event.from_user.id, message.chat.id)
        entry = get_screen(key)
        if is_navigation(event):
            entry.version += 1
        entry.active += 1
        token = current_screen.set((key, entry, entry.version))
        old_state = data.get('state')
        if old_state:
            data['state'] = GuardedState(old_state)
        try:
            return await handler(event, data)
        except SupersededScreen:
            # A newer screen owns the card. Financial work already committed
            # remains recorded; no older UI or FSM state is replayed.
            return None
        finally:
            if old_state:
                data['state'] = old_state
            current_screen.reset(token)
            entry.active -= 1
