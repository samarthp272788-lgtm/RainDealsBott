import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from sqlalchemy import create_engine, text
from services.input_forms import quantity, email, phone, field_value, is_cancel, InputError
from services import performance
from pathlib import Path
import ast
import sys
from unittest.mock import Mock, patch


class FormsTests(unittest.TestCase):
    def test_long_evidence_does_not_truncate_replacement_details(self):
        from services.warranty import summary_text, request_text
        from html import unescape
        import re
        ctx = dict(order_id=21, days=7, expires=None, error=None, request=dict(status='completed',
                   reason='R'*1500, evidence={'kind':'text', 'value':'E'*2000}, resolution='X'*2990+'FINAL_DATA'))
        summary = unescape(re.sub(r'<[^>]+>', '', summary_text(ctx)))
        details = unescape(re.sub(r'<[^>]+>', '', request_text(ctx)))
        self.assertLessEqual(len(summary), 4096)
        self.assertTrue(summary.endswith('FINAL_DATA'))
        self.assertLessEqual(len(details), 4096)
        self.assertIn('R'*1500, details)
        self.assertIn('E'*2000, details)

    def test_quantity_limits_and_non_numeric_input(self):
        self.assertEqual(quantity(' 3 ', 4), 3)
        for value in ('0', '-1', '5', '1.5', '1e2', 'a', '9'*10000):
            with self.assertRaises(InputError):
                quantity(value, 4)

    def test_email_phone_and_cancel_are_shared_and_strict(self):
        self.assertEqual(email(' Buyer@Example.COM '), 'Buyer@example.com')
        self.assertEqual(phone('+91 98765 43210'), '+91 98765 43210')
        for value in ('x@', '@example.com', 'hello there@example.com', '.x@example.com', 'x..y@example.com'):
            with self.assertRaises(InputError):
                email(value)
        for value in ('call 987654321', '123', '1'*18):
            with self.assertRaises(InputError):
                phone(value)
        self.assertTrue(is_cancel('/cancel@MyBot'))
        self.assertTrue(is_cancel(' Cancel '))
        self.assertFalse(is_cancel('cancel my order please'))
        self.assertFalse(is_cancel('cancel@example.com'))

    def test_file_and_contact_fields_require_the_right_input(self):
        message = SimpleNamespace(photo=[], contact=None, text='wrong input')
        for kind in ('photo', 'contact', 'unsupported'):
            with self.assertRaises(InputError):
                field_value(message, kind)
        message.photo = [SimpleNamespace(file_id='one'), SimpleNamespace(file_id='two')]
        self.assertEqual(field_value(message, 'photo'), {'kind': 'photo', 'file_id': 'two'})


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        with performance._lock:
            performance._metrics.clear()

    def test_database_observations_retain_no_sql_or_parameters(self):
        engine = create_engine('sqlite://')
        performance.install_database_observers(engine)
        performance.install_database_observers(engine)  # Installing twice must not double count.
        try:
            token = performance.operation.set('products_cat')
            try:
                with engine.connect() as db:
                    db.execute(text('SELECT :secret'), {'secret': 'SECRET_ACCOUNT_VALUE'})
                    with self.assertRaises(Exception):
                        db.execute(text('SELECT * FROM table_that_does_not_exist'))
            finally:
                performance.operation.reset(token)
            rows = performance.snapshot()
            self.assertEqual(rows[0]['count'], 2)
            self.assertEqual(rows[0]['failed'], 1)
            self.assertEqual(rows[0]['label'], 'products_cat')
            self.assertNotIn('SECRET_ACCOUNT_VALUE', str(rows))
            self.assertNotIn('table_that', str(rows))
        finally:
            engine.dispose()

    def test_telegram_latency_excludes_long_polling(self):
        async def run():
            method = type('SendMessage', (), {})()
            await performance.TelegramTimings()(AsyncMock(return_value=True), None, method)
            await performance.TelegramTimings()(AsyncMock(return_value=True), None, type('GetUpdates', (), {})())
        asyncio.run(run())
        self.assertEqual(len(performance.snapshot()), 1)
        self.assertEqual(performance.snapshot()[0]['label'], 'SendMessage')

    def test_update_latency_uses_safe_operation_names(self):
        async def run():
            event = SimpleNamespace(data='product_123_SECRET_VALUE')
            await performance.UpdateTimings()(AsyncMock(return_value=True), event, {})
        asyncio.run(run())
        self.assertEqual(performance.snapshot()[0]['label'], 'product')
        self.assertNotIn('SECRET_VALUE', str(performance.snapshot()))
        self.assertEqual(performance.operation.get(), 'background')

    def test_observation_storage_is_bounded(self):
        for index in range(300):
            performance.observe('unit', f'op-{index}', .1)
        self.assertEqual(len(performance.snapshot()), 200)
        for _ in range(400):
            performance.observe('unit', 'last', .1)
        self.assertEqual(len(performance._metrics[('unit', 'last')]['samples']), 200)


class FormHandlerTests(unittest.IsolatedAsyncioTestCase):
    def handler(self, file, name, namespace):
        source = (Path(__file__).resolve().parents[1]/file).read_text(encoding='utf-8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        node.decorator_list = []
        namespace.update(Message=object, CallbackQuery=object, FSMContext=object)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<form-handler>', 'exec'), namespace)
        return namespace[name]

    async def test_quantity_cancel_clears_state_without_fetching_or_buying(self):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        update = AsyncMock()
        fetch = Mock(side_effect=AssertionError('Cancellation must not fetch products'))
        handler = self.handler('handlers/products.py', 'receive_qty', dict(is_cancel=is_cancel, update_card=update,
                                  InlineKeyboardButton=InlineKeyboardButton, InlineKeyboardMarkup=InlineKeyboardMarkup, _fetch_product=fetch))
        state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_product_id':21, '_card_chat_id':7, '_card_message_id':22}),
                                clear=AsyncMock())
        await handler(SimpleNamespace(text='/cancel'), state)
        state.clear.assert_awaited_once()
        fetch.assert_not_called()
        self.assertEqual(update.await_args.kwargs['message_id'], 22)
        self.assertEqual(update.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data, 'product_21')

    async def test_policy_cancel_button_clears_input_before_returning_to_product(self):
        refresh = AsyncMock()
        handler = self.handler('handlers/bot_operations.py', 'cancel_warranty_policy', {'ADMIN_IDS':[101]})
        callback = SimpleNamespace(from_user=SimpleNamespace(id=101), data='warranty_cancelconfig_21', answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock())
        with patch.dict(sys.modules, {'handlers.admin_product_manage':SimpleNamespace(_refresh_manage_panel=refresh)}):
            await handler(callback, state)
        state.clear.assert_awaited_once()
        refresh.assert_awaited_once_with(callback, 21)
