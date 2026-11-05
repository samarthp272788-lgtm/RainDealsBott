import asyncio
from datetime import datetime, timedelta, timezone
import unittest
import sys
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from aiogram.types import Chat, Message, User
from middleware.session_timeout import CallbackSessionTimeoutMiddleware
from services.session_resume import make_snapshot
import services.session_resume as resume
from handlers.restart import restart_cmd, stop_restart


class State:
    def __init__(self, state=None, data=None):
        self.state, self.data = state, data or {}
    async def get_state(self):
        return self.state
    async def get_data(self):
        return self.data.copy()
    async def set_state(self, value):
        self.state = value
    async def set_data(self, value):
        self.data = value.copy()
    async def update_data(self, **kwargs):
        self.data.update(kwargs)
    async def clear(self):
        self.state, self.data = None, {}


def message(text):
    return Message(message_id=4, date=datetime.now(timezone.utc), chat=Chat(id=7, type='private'),
                   from_user=User(id=7, is_bot=False, first_name='Buyer'), text=text).as_(Mock())


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        async def fresh_card(msg, state, data):
            await state.update_data(_card_chat_id=7, _card_message_id=20)
            return msg.model_copy(update={'message_id': 20}).as_(msg.bot)
        patcher = patch('handlers.restart.begin_resumed_card', AsyncMock(side_effect=fresh_card))
        self.begin_card = patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_excludes_passwords_and_unsafe_actions(self):
        snapshot = make_snapshot('PurchaseStates:waiting_qty', {'pending_product_id': 9, 'password': 'private'}, 'confirm_buy_9')
        self.assertEqual(snapshot['data'], {'pending_product_id': 9})
        self.assertIsNone(snapshot['navigation'])
        self.assertIsNone(make_snapshot('AddProvider:api_key', {'api_key': 'secret'}))
        self.assertIsNone(make_snapshot(None, {}, 'deposit_bep20'))  # Never replay creation.
        self.assertIsNone(make_snapshot(None, {}, 'confirm_buy_9'))

    async def test_restart_restores_quantity_without_replaying_purchase(self):
        snapshot = make_snapshot('PurchaseStates:waiting_qty', {'pending_product_id': 9})
        state = State()
        with patch('handlers.restart.load_resume', AsyncMock(return_value=snapshot)), \
             patch('handlers.restart.save_resume') as save, \
             patch('handlers.restart.answer_card', AsyncMock()) as answer:
            await restart_cmd(message('/restart'), state)
        self.assertEqual(state.state, 'PurchaseStates:waiting_qty')
        self.assertEqual(state.data['pending_product_id'], 9)
        self.assertIn('quantity', answer.call_args.args[1])
        save.assert_called_once()

    async def test_restart_confirmation_rechecks_stock_and_does_not_buy(self):
        snapshot = make_snapshot('PurchaseStates:waiting_qty', {'pending_product_id': 9, 'qty_9': 3})
        state = State()
        buy = Mock()
        product = SimpleNamespace(is_active=True, price=1)
        products = SimpleNamespace(_fetch_product=Mock(return_value=product), _get_custom_price=Mock(return_value=None),
                                   _get_max_qty=lambda p: 4, _real_stock=lambda p: 2,
                                   _qty_text=Mock(return_value='Updated confirmation'), _confirm_keyboard=lambda *args: None,
                                   _do_purchase=buy)
        with patch.dict(sys.modules, {'handlers.products': products}), \
             patch('handlers.restart.load_resume', AsyncMock(return_value=snapshot)), \
             patch('handlers.restart.save_resume'), patch('handlers.restart.answer_card', AsyncMock()) as answer:
            await restart_cmd(message('/restart'), state)
        self.assertEqual(answer.call_args.args[1], 'Updated confirmation')
        products._fetch_product.assert_called_once_with(9)
        buy.assert_not_called()

    async def test_expired_invoice_requires_explicit_renewal(self):
        snapshot = make_snapshot('DepositState:waiting_txid', {'deposit_id': 55, 'network': 'BEP20'})
        deposit = SimpleNamespace(_load_invoice_for_resume=Mock(return_value={
            'id': 55, 'expires_at': (datetime.utcnow()-timedelta(minutes=1)).isoformat(), 'tx_hash': None}),
            resume_deposit=AsyncMock(), NETWORK_CALLBACKS={'deposit_bep20': 'BEP20'})
        state = State()
        with patch.dict(sys.modules, {'handlers.deposit': deposit}), \
             patch('handlers.restart.load_resume', AsyncMock(return_value=snapshot)), \
             patch('handlers.restart.save_resume') as save, patch('handlers.restart.answer_card', AsyncMock()) as answer:
            await restart_cmd(message('/restart'), state)
        self.assertIsNone(state.state)
        deposit.resume_deposit.assert_not_awaited()
        save.assert_called_once_with((7, 7), None)
        self.assertIn('expired', answer.call_args.args[1])

    async def test_idle_input_does_not_reach_payment_handler_until_restart(self):
        middleware = CallbackSessionTimeoutMiddleware()
        middleware._sessions[(7, 7)] = (datetime.now().timestamp() - 500, 420)
        state = State('DepositState:waiting_txid', {'deposit_id': 55})
        handler = AsyncMock()
        with patch('utils.ui.answer_card', AsyncMock()) as answer:
            await middleware(handler, message('0xpaymentreference'), {'state': state})
        handler.assert_not_awaited()
        self.assertIn('/restart', answer.call_args.args[1])
        self.assertEqual(state.data['deposit_id'], 55)

    async def test_restart_command_passes_expiry_gate(self):
        middleware = CallbackSessionTimeoutMiddleware()
        middleware._sessions[(7, 7)] = (datetime.now().timestamp() - 500, 420)
        handler = AsyncMock()
        with patch('middleware.session_timeout.save_resume'):
            await middleware(handler, message('/restart'), {'state': State('PurchaseStates:waiting_qty')})
        handler.assert_awaited_once()

    async def test_start_cancels_restart_before_saved_menu_can_return(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def loading(_key):
            entered.set()
            await release.wait()
            return make_snapshot(None, {}, 'product_9')
        with patch('handlers.restart.load_resume', AsyncMock(side_effect=loading)), \
             patch('handlers.restart.answer_card', AsyncMock()) as answer:
            running = asyncio.create_task(restart_cmd(message('/restart'), State()))
            await asyncio.wait_for(entered.wait(), 0.5)
            await stop_restart((7, 7))
            release.set()
            await asyncio.wait_for(running, 0.5)
        self.begin_card.assert_not_awaited()
        answer.assert_not_awaited()

    async def test_command_middleware_does_not_rewrite_cleared_resume_draft(self):
        middleware = CallbackSessionTimeoutMiddleware()
        state = State('PurchaseStates:waiting_qty', {'pending_product_id': 9})
        with patch('middleware.session_timeout.save_resume') as save:
            await middleware(AsyncMock(), message('/restart'), {'state': state})
            await middleware(AsyncMock(), message('/start'), {'state': state})
        save.assert_not_called()

    async def test_pending_snapshot_cannot_overwrite_start_clear(self):
        key = (7, 7)
        writes = []
        with patch('services.session_resume._persist', side_effect=lambda k, v: writes.append(v)):
            resume.save_resume(key, {'state': 'PurchaseStates:waiting_qty', 'data': {}, 'navigation': None})
            resume.save_resume(key, None)
            task = resume._writers[key]
            await task
        self.assertEqual(writes, [None])
        self.assertIsNone(await resume.load_resume(key))
        resume._cache.clear()

    async def test_expired_draft_is_not_restored(self):
        key = (8, 8)
        resume._cache[key] = (datetime.utcnow() - timedelta(days=2), {'state': 'PurchaseStates:waiting_qty'})
        self.assertIsNone(await resume.load_resume(key))
        resume._cache.clear()

    async def test_restart_without_saved_step_opens_home(self):
        start = SimpleNamespace(start_cmd=AsyncMock())
        state = State()
        with patch.dict(sys.modules, {'handlers.start': start}), \
             patch('handlers.restart.load_resume', AsyncMock(return_value=None)):
            await restart_cmd(message('/restart'), state)
        start.start_cmd.assert_awaited_once()
        self.begin_card.assert_not_awaited()

    async def test_saved_product_page_is_restored_with_wipe_in_flag(self):
        snapshot = make_snapshot(None, {'_card_chat_id': 7, '_card_message_id': 11}, 'product_9')
        products = SimpleNamespace(**{name: AsyncMock() for name in (
            'products_menu', 'products_all', 'products_category', 'product_info')})
        orders = SimpleNamespace(my_orders=AsyncMock(), order_detail=AsyncMock())
        deposit = SimpleNamespace(deposit_menu=AsyncMock())
        with patch.dict(sys.modules, {'handlers.products': products, 'handlers.orders': orders, 'handlers.deposit': deposit}), \
             patch('handlers.restart.load_resume', AsyncMock(return_value=snapshot)), \
             patch('handlers.restart.save_resume') as save:
            await restart_cmd(message('/restart'), State())
        callback = products.product_info.call_args.args[0]
        self.assertEqual(callback.data, 'product_9')
        self.assertTrue(callback.resume_wipe_in)
        products.products_menu.assert_not_awaited()
        self.assertEqual(save.call_args.args[1]['navigation'], 'product_9')

    async def test_wipe_in_sends_saved_menu_as_new_card_instead_of_edit(self):
        from utils import ui
        actions = []
        fresh = SimpleNamespace(chat=SimpleNamespace(id=7), message_id=30)
        async def delete():
            actions.append('wipe_out_marker')
        async def send(text, **kwargs):
            actions.append(('wipe_in_menu', text))
            return fresh
        marker = SimpleNamespace(chat=SimpleNamespace(id=7), message_id=20,
                                 delete=AsyncMock(side_effect=delete), answer=AsyncMock(side_effect=send), edit_text=AsyncMock())
        callback = SimpleNamespace(message=marker, from_user=SimpleNamespace(id=7), resume_wipe_in=True)
        with patch.dict(ui._CARD_LOCATIONS, {}, clear=True):
            await ui.show(callback, '<b>Saved product page</b>', parse_mode='HTML')
            self.assertEqual(actions, ['wipe_out_marker', ('wipe_in_menu', '<b>Saved product page</b>')])
            marker.edit_text.assert_not_awaited()
            self.assertEqual(ui.get_card_location(7, 7), (7, 30))

    async def test_wipe_deletes_only_tracked_cards_and_keeps_draft(self):
        from utils import ui
        bot = AsyncMock()
        command = message('/restart').as_(bot)
        fresh = command.model_copy(update={'message_id': 20}).as_(bot)
        bot.return_value = fresh
        state = State('DepositState:waiting_txid', {'deposit_id': 55})
        data = {'deposit_id': 55, 'network': 'BEP20', '_card_chat_id': 7,
                '_card_message_id': 11, 'invoice_photo_message_id': 12}
        with patch.dict(ui._CARD_LOCATIONS, {(7, 7): (7, 22)}, clear=True):
            card = await ui.begin_resumed_card(command, state, data)
            deleted = {c.kwargs['message_id'] for c in bot.delete_message.call_args_list}
            self.assertEqual(deleted, {4, 11, 12, 22})
            self.assertEqual(card.message_id, 20)
            self.assertEqual(ui.get_card_location(7, 7), (7, 20))
            self.assertEqual(state.data['deposit_id'], 55)
            self.assertEqual(state.data['_card_message_id'], 20)
            self.assertNotIn('invoice_photo_message_id', state.data)

    async def test_qr_replacement_removes_continuing_placeholder(self):
        from utils import ui
        bot = AsyncMock()
        command = message('/restart').as_(bot)
        placeholder = command.model_copy(update={'message_id': 20}).as_(bot)
        state = State('DepositState:waiting_txid', {'deposit_id': 55, '_card_chat_id': 7, '_card_message_id': 30})
        with patch.dict(ui._CARD_LOCATIONS, {(7, 7): (7, 20)}, clear=True):
            await ui.finish_resumed_card(command, state, placeholder)
            bot.delete_message.assert_awaited_once_with(chat_id=7, message_id=20)
            self.assertEqual(ui.get_card_location(7, 7), (7, 30))

    async def test_start_replaces_home_and_newer_resumed_cards_with_one_menu(self):
        from utils import ui
        bot = AsyncMock()
        command = message('/start').as_(bot)
        fresh = command.model_copy(update={'message_id': 50}).as_(bot)
        bot.return_value = fresh
        with patch.dict(ui._CARD_LOCATIONS, {(7, 7): (7, 30)}, clear=True):
            await ui.update_card(command, text='Home', owner_id=7, force_new_card=True,
                                 chat_id=7, message_id=11, cleanup_cards=[(7, 40), (99, 123)])
            deleted = {(c.kwargs['chat_id'], c.kwargs['message_id']) for c in bot.delete_message.call_args_list}
            self.assertEqual(deleted, {(7, 11), (7, 30), (7, 40)})
            self.assertEqual(ui.get_card_location(7, 7), (7, 50))
            sent = [c.args[0] for c in bot.await_args_list if type(c.args[0]).__name__ == 'SendMessage']
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].text, 'Home')


if __name__ == '__main__':
    unittest.main()
