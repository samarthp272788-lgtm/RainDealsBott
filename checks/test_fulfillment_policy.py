"""Stock allocation and checkout regression tests without production DB access."""
import ast
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest
from services.warranty import start_coverage
from services.fulfillment_policy import plan_delivery, order_capacity, manual_allowed

ROOT = Path(__file__).resolve().parents[1]


def product(**changes):
    values = dict(id=1, source='own', name='Test', icon='T', price=Decimal('1'), stock=1000,
                  file_content='a:1\nb:2', delivery_type='hybrid', manual_fallback=True,
                  preorder=False, is_active=True, required_info=None, low_stock_threshold=3,
                  bulk_pricing=None, delivery_instruction=None)
    values.update(changes)
    return SimpleNamespace(**values)


class Field:
    def __eq__(self, other):
        return self
    def in_(self, values):
        return self


class User:
    telegram_id = Field()


class Product:
    id = Field()


class Order(SimpleNamespace):
    telegram_id = product_id = status = refunded = id = checkout_key = Field()


class Submission(SimpleNamespace):
    pass


def extract(path, name, ns):
    tree = ast.parse((ROOT/path).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<fulfillment>', 'exec'), ns)
    return ns[name]


class Query:
    def __init__(self, value):
        self.value = value
    def filter(self, *args):
        return self
    def filter_by(self, **kwargs):
        return self
    def with_for_update(self):
        return self
    def first(self):
        return self.value
    def count(self):
        return 0


class CheckoutTests(unittest.TestCase):
    def checkout(self, p, quantity, replay=False):
        user = SimpleNamespace(telegram_id=7, balance=Decimal('100'), total_orders=0,
                               total_spent=Decimal('0'), referred_by=None, is_banned=False)
        added = []
        class DB:
            def query(self, model):
                return Query(user if model is User else p if model is Product else added[0] if added else None)
            def add(self, obj):
                obj.id = 9
                added.append(obj)
            def flush(self):
                pass
        @contextmanager
        def transaction():
            yield DB()
        tree = ast.parse((ROOT/'handlers/products.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_do_purchase')
        node.decorator_list = []
        ns = dict(transaction=transaction, User=User, Product=Product, Order=Order,
                  Decimal=Decimal, _money=lambda v: Decimal(str(v)).quantize(Decimal('.00000001')),
                  _active_custom_price=lambda *args: None, _get_bulk_price=lambda *args: None,
                  plan_delivery=plan_delivery, order_capacity=order_capacity, manual_allowed=manual_allowed,
                  DEFAULT_LOW_STOCK_THRESHOLD=3, invalidate_products_cache=lambda: None, enqueue=lambda *args, **kwargs: None, record_event=lambda *args, **kwargs: None, start_coverage=start_coverage)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<checkout>', 'exec'), ns)
        result = ns['_do_purchase'](7, 1, quantity, None, 'e'*64 if replay else None)
        if replay:
            repeated = ns['_do_purchase'](7, 1, quantity, None, 'e'*64)
            self.assertEqual(repeated, {'duplicate_order_id': result['order_id']})
        return result, user, added

    def test_replayed_confirmation_cannot_debit_or_allocate_stock_again(self):
        p = product()
        _, user, orders = self.checkout(p, 3, replay=True)
        self.assertEqual(user.balance, Decimal('97'))
        self.assertEqual(user.total_orders, 1)
        self.assertEqual(p.stock, 997)
        self.assertEqual(len(orders), 1)

    def test_hybrid_two_ready_one_manual_and_one_wallet_charge(self):
        p = product()
        result, user, orders = self.checkout(p, 3)
        self.assertEqual(result['delivered_accounts'], ['a:1', 'b:2'])
        self.assertEqual(result['manual_pending_quantity'], 1)
        self.assertEqual(result['status'], 'pending_manual')
        self.assertEqual(user.balance, Decimal('97'))
        self.assertEqual(user.total_orders, 1)
        self.assertEqual(p.stock, 997)
        self.assertIsNone(p.file_content)
        self.assertEqual(orders[0].automatic_quantity, 2)
        self.assertEqual(orders[0].stock_deducted_quantity, 3)

    def test_disabled_fallback_rejects_before_wallet_or_inventory_mutation(self):
        p = product(manual_fallback=False)
        result, user, orders = self.checkout(p, 3)
        self.assertIn('error', result)
        self.assertEqual(user.balance, Decimal('100'))
        self.assertEqual(p.file_content, 'a:1\nb:2')
        self.assertEqual(p.stock, 1000)
        self.assertEqual(orders, [])

    def test_automatic_fallback_without_accounts_routes_order_to_manual(self):
        p = product(delivery_type='automatic', file_content=None)
        result, user, orders = self.checkout(p, 3)
        self.assertEqual(result['status'], 'pending_manual')
        self.assertEqual(orders[0].delivery_type, 'manual')
        self.assertEqual(orders[0].manual_pending_quantity, 3)
        self.assertEqual(p.delivery_type, 'automatic')
        self.assertEqual(p.stock, 997)

    def test_strict_automatic_stock_comes_from_uploaded_accounts(self):
        p = product(delivery_type='automatic', manual_fallback=False)
        self.assertEqual(order_capacity(p), 2)
        result, _, orders = self.checkout(p, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(p.stock, 0)
        self.assertEqual(orders[0].manual_pending_quantity, 0)

    def test_requirements_hold_automatic_accounts_until_submission(self):
        p = product(required_info=[{'label': 'Email', 'type': 'email'}])
        result, _, orders = self.checkout(p, 3)
        self.assertEqual(result['status'], 'pending_info')
        self.assertEqual(result['delivered_accounts'], [])
        self.assertIsNone(orders[0].delivered_account)
        self.assertEqual(orders[0].reserved_delivery, 'a:1\nb:2')
        self.assertEqual(orders[0].manual_pending_quantity, 1)

    def test_information_submission_releases_partial_delivery_but_keeps_manual_remainder(self):
        _, _, orders = self.checkout(product(required_info=[{'label': 'Email', 'type': 'email'}]), 3)
        order = orders[0]
        order.assigned_admin_id = None
        order.assigned_at = None
        class DB:
            def query(self, model):
                return Query(order if model is Order else None)
            def add(self, obj):
                pass
            def commit(self):
                pass
            def close(self):
                pass
        fn = extract('handlers/orders.py', '_save_order_requirement_submission',
                     dict(SessionLocal=DB, Order=Order, OrderRequirementSubmission=Submission, enqueue=lambda *args, **kwargs: None, record_event=lambda *args, **kwargs: None, start_coverage=start_coverage))
        result = fn(9, 7, {'Email': {'kind': 'text', 'value': 'buyer@example.com'}})
        self.assertEqual(result['status'], 'pending_manual')
        self.assertEqual(result['delivery'], 'a:1\nb:2')
        self.assertIsNone(order.reserved_delivery)
        self.assertEqual(order.manual_pending_quantity, 1)

    def test_manual_completion_appends_without_overwriting_automatic_delivery(self):
        _, _, orders = self.checkout(product(), 3)
        order = orders[0]
        order.assigned_admin_id = None
        order.assigned_at = None
        class DB:
            def query(self, model):
                return Query(order if model is Order else None)
            def add(self, obj):
                pass
        @contextmanager
        def transaction():
            yield DB()
        from unittest.mock import Mock
        fn = extract('handlers/admin_orders.py', '_do_deliver',
                     dict(transaction=transaction, Order=Order, ApiOrder=type('ApiOrder', (), {'id': Field(), 'order_id': Field()}),
                          AdminAuditLog=Submission, logger=Mock(), datetime=__import__("datetime").datetime, enqueue=lambda *args, **kwargs: None, record_event=lambda *args, **kwargs: None, start_coverage=start_coverage))
        result = fn(9, 'c:3', 123)
        self.assertNotIn('error', result)
        self.assertEqual(order.delivered_account, 'a:1\nb:2\nc:3')
        self.assertEqual(order.status, 'completed')
        self.assertEqual(order.manual_pending_quantity, 0)

    def test_partial_refund_restores_capacity_and_only_automatic_accounts(self):
        p = product()
        _, buyer, orders = self.checkout(p, 3)
        order = orders[0]
        order.delivered_account += '\nmanual instruction, not inventory'
        class DB:
            def query(self, model):
                return Query(order if model is Order else buyer if model is User else p if model is Product else None)
            def add(self, obj):
                pass
        @contextmanager
        def transaction():
            yield DB()
        from unittest.mock import Mock
        fn = extract('handlers/admin_orders.py', '_do_refund',
                     dict(transaction=transaction, Order=Order, User=User, Product=Product, AdminAuditLog=Submission,
                          Decimal=Decimal, _money=lambda v: Decimal(str(v)), logger=Mock(), enqueue=lambda *args, **kwargs: None, record_event=lambda *args, **kwargs: None, start_coverage=start_coverage))
        self.assertNotIn('error', fn(9, 123))
        self.assertEqual(p.stock, 1000)
        self.assertEqual(p.file_content, 'a:1\nb:2')
        self.assertEqual(buyer.balance, Decimal('100'))
        self.assertIn('error', fn(9, 123))  # Second refund must not credit again.

    def test_manual_capacity_does_not_require_uploaded_accounts(self):
        p = product(delivery_type='manual', manual_fallback=False, file_content=None)
        self.assertEqual(plan_delivery(p, 1000).manual_quantity, 1000)
        with self.assertRaises(ValueError):
            plan_delivery(p, 1001)

    def test_preorder_requires_permission_and_reserves_no_ready_accounts(self):
        p = product(stock=0, file_content=None, preorder=True)
        plan = plan_delivery(p, 3)
        self.assertTrue(plan.preorder)
        self.assertEqual(plan.stock_after, 0)
        p.manual_fallback = False
        with self.assertRaises(ValueError):
            plan_delivery(p, 3)


if __name__ == '__main__':
    unittest.main()
