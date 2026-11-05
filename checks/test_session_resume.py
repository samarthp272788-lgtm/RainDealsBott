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
from handlers.restart import restart_cmd


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


if __name__ == '__main__':
    unittest.main()
