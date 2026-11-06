"""Exercise the real form handlers without Telegram, secrets or database access."""
import ast
import asyncio
from decimal import Decimal
from html import escape
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from states.product_states import AddProduct
from services import warranty
from services.input_forms import InputError, is_cancel, text_value, field_value

ROOT = Path(__file__).resolve().parents[1]


class State:
    def __init__(self, value=None, **data):
        self.value, self.data = value, data
    async def get_state(self):
        return self.value
    async def get_data(self):
        return dict(self.data)
    async def set_state(self, value):
        self.value = value.state
    async def update_data(self, **data):
        self.data.update(data)
    async def clear(self):
        self.value, self.data = None, {}


def load(file, names, namespace):
    namespace.update(Message=object, CallbackQuery=object, FSMContext=object)
    tree = ast.parse((ROOT/file).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, (ast.AsyncFunctionDef,ast.FunctionDef)) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes,type_ignores=[]), '<aftercare-form>', 'exec'), namespace)
    return SimpleNamespace(**{name:namespace[name] for name in names})


class AftercareFormsTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_asks_policy_before_delivery_and_import_is_not_saved_early(self):
        show = AsyncMock()
        save = AsyncMock()
        ns = dict(ADMIN_IDS=[101], AddProduct=AddProduct, answer_card=show,
                  InlineKeyboardButton=InlineKeyboardButton, InlineKeyboardMarkup=InlineKeyboardMarkup,
                  _esc=escape, _save_reseller_product=save)
        h = load('handlers/admin_products.py', ['product_description','_ask_product_aftercare',
                  'product_aftercare_choice','product_aftercare_days','product_aftercare_prompt','_continue_aftercare_product'], ns)
        state = State(source='own', name='Fixture')
        message = SimpleNamespace(text='Product description', from_user=SimpleNamespace(id=101))
        await h.product_description(message,state)
        self.assertEqual(state.value, AddProduct.aftercare_policy.state)
        choices = [b.callback_data for row in show.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertTrue({'addcare_warranty','addcare_refund','addcare_skip'} <= set(choices))
        callback = SimpleNamespace(from_user=message.from_user,data='addcare_skip',answer=AsyncMock())
        await h.product_aftercare_choice(callback,state)
        self.assertEqual(state.value, AddProduct.delivery_type.state)
        self.assertEqual(state.data['aftercare_policy'],'skip')
        save.assert_not_awaited()
        state = State(AddProduct.aftercare_policy.state,source='reseller',price=2)
        callback.data = 'addcare_refund'
        await h.product_aftercare_choice(callback,state)
        save.assert_not_awaited()
        self.assertEqual(state.value,AddProduct.aftercare_days.state)
        message.text = '0'
        await h.product_aftercare_days(message,state)
        save.assert_not_awaited()
        message.text = '30'
        await h.product_aftercare_days(message,state)
        self.assertEqual(save.await_args.args[2]['subscription_days'],30)
        self.assertEqual(save.await_args.args[2]['aftercare_policy'],'refund')
        message.text = '/cancel'
        await state.set_state(AddProduct.aftercare_policy)
        await h.product_aftercare_prompt(message,state)
        self.assertIsNone(state.value)
        self.assertEqual(save.await_count,1)

    async def test_claim_requires_deactivation_proof_and_confirmation_before_submission(self):
        inputs = SimpleNamespace(**{name:SimpleNamespace(state='Inputs:'+name) for name in
                                    ('reason','deactivated','evidence','confirmation')})
        ctx = dict(error=None,request=None,policy='refund',quote=dict(paid=Decimal('30'),subscription_days=30,
                   used_days=3,remaining_days=27,refund_amount=Decimal('21.6')))
        submit = Mock(return_value=None)
        service = SimpleNamespace(context=Mock(return_value=ctx),submit=submit,quote_text=warranty.quote_text)
        show, render = AsyncMock(), AsyncMock()
        ns = dict(asyncio=asyncio, Inputs=inputs, warranty=service, escape=escape, InputError=InputError,
                  text_value=text_value, field_value=field_value, is_cancel=is_cancel, answer_card=show,
                  render_warranty=render, back=lambda *args:None, keyboard=lambda rows:rows, Button=InlineKeyboardButton)
        h = load('handlers/bot_operations.py',['warranty_input','claim_confirmation','submit_claim','claim_confirmation_input'],ns)
        state = State(inputs.reason.state,warranty_order=21,warranty_mode='refund')
        message = SimpleNamespace(from_user=SimpleNamespace(id=7),text='Account shows deactivated.',photo=[])
        await h.warranty_input(message,state)
        self.assertEqual(state.value,inputs.deactivated.state)
        message.text = '6 November, 2 PM IST'
        await h.warranty_input(message,state)
        self.assertEqual(state.value,inputs.evidence.state)
        message.text = 'I have no screenshot yet'
        await h.warranty_input(message,state)
        self.assertEqual(state.value,inputs.evidence.state)
        self.assertIn('screenshot',show.await_args.args[1])
        submit.assert_not_called()
        message.photo = [SimpleNamespace(file_id='proof-photo')]
        message.text = None
        await h.warranty_input(message,state)
        self.assertEqual(state.value,inputs.confirmation.state)
        self.assertIn('$21.60000000',show.await_args.args[1])
        self.assertIn('2 PM IST',show.await_args.args[1])
        submit.assert_not_called()
        callback = SimpleNamespace(from_user=message.from_user,data='warranty_submit_21',answer=AsyncMock())
        await h.submit_claim(callback,state)
        self.assertEqual(submit.call_args.args,(21,7,'Account shows deactivated.',{'kind':'photo','file_id':'proof-photo'},'6 November, 2 PM IST','21.6'))
        self.assertIsNone(state.value)
        submit.reset_mock()
        await h.submit_claim(callback,state)
        submit.assert_not_called()
        self.assertIn('expired',callback.answer.await_args.args[0])
        state = State(inputs.confirmation.state,warranty_order=21)
        message.text = '/cancel'
        await h.claim_confirmation_input(message,state)
        self.assertIsNone(state.value)
        render.assert_awaited_with(message,21)
        submit.assert_not_called()
