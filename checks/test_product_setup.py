import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from states.product_states import AddProduct


class State:
    def __init__(self, **data):
        self.data, self.state = data, None
    async def get_data(self):
        return self.data.copy()
    async def update_data(self, **data):
        self.data.update(data)
    async def set_state(self, state):
        self.state = state


class ProductSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1]/'handlers/admin_products.py').read_text(encoding='utf-8'))
        names = {'product_delivery', 'product_manual_fallback', 'product_stock_capacity', '_finish_own_product'}
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.ns = dict(Message=Message, FSMContext=FSMContext, ADMIN_IDS=[7], AddProduct=AddProduct,
                       answer_card=AsyncMock(), _ask_delivery_instruction=AsyncMock())
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<product-setup>', 'exec'), self.ns)

    def message(self, text):
        return SimpleNamespace(text=text, from_user=SimpleNamespace(id=7))

    async def test_automatic_mode_asks_admin_before_allowing_fallback(self):
        state = State()
        await self.ns['product_delivery'](self.message('automatic'), state)
        self.assertEqual(state.state, AddProduct.manual_fallback)
        self.assertNotIn('manual_fallback', state.data)
        await self.ns['product_manual_fallback'](self.message('no'), state)
        self.assertFalse(state.data['manual_fallback'])
        self.ns['_ask_delivery_instruction'].assert_awaited_once()

    async def test_empty_strict_inventory_offers_manual_switch_before_saving(self):
        state = State(delivery_type='automatic', manual_fallback=False, draft_accounts=[])
        await self.ns['_finish_own_product'](self.message('skip'), state)
        self.assertEqual(state.state, AddProduct.empty_inventory)
        self.assertIn('Switch to manual', self.ns['answer_card'].call_args.args[1])

    async def test_capacity_1000_accepts_only_two_uploaded_accounts(self):
        state = State(delivery_type='hybrid', manual_fallback=True, draft_accounts=['a:1', 'b:2'])
        await self.ns['_finish_own_product'](self.message('accounts'), state)
        self.assertEqual(state.state, AddProduct.stock_capacity)
        self.ns['_finish_own_product'] = AsyncMock()
        await self.ns['product_stock_capacity'](self.message('1000'), state)
        self.assertEqual(state.data['stock_capacity'], 1000)
        self.ns['_finish_own_product'].assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
