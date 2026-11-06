"""Use an isolated SQLite database. No production config, network or payments."""
import importlib.util
import ast
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import Boolean, Column, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from services.checkout_actions import action_key, claim_checkout, finish_checkout, recover_checkout
from services.order_workflow import change_work, queue_orders, read_timeline, record_event, work_details

ROOT = Path(__file__).resolve().parents[1]


def fixture_module(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.engine = create_engine('sqlite:///' + str(Path(cls.temp.name) / 'test.db'),
                                   connect_args={'check_same_thread': False, 'timeout': 20})
        cls.Base = declarative_base()
        class Provider(cls.Base):
            __tablename__ = 'providers'
            id = Column(Integer, primary_key=True)
            name = Column(String(255), nullable=False, default='Test provider')
            api_key = Column(String(255), nullable=True)
            api_secret = Column(String(255), nullable=True)
            is_active = Column(Boolean, nullable=False, default=True)
        class Product(cls.Base):
            __tablename__ = 'products'
            id = Column(Integer, primary_key=True)
            warranty_days = Column(Integer, nullable=False, default=0)
            aftercare_policy = Column(String(16), nullable=True)
            subscription_days = Column(Integer, nullable=False, default=0)
        cls.products, cls.providers = Product, Provider
        cls.factory = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)
        class SQLAdapter:
            def __init__(self, session):
                self.session = session
            def __getattr__(self, key):
                return getattr(self.session, key)
            def execute(self, statement, params=None):
                # Production is MySQL/TiDB. Preserve INSERT IGNORE semantics in SQLite.
                sql = str(statement).replace('INSERT IGNORE', 'INSERT OR IGNORE').replace('UTC_TIMESTAMP()', 'CURRENT_TIMESTAMP').replace(' FOR UPDATE', '')
                return self.session.execute(text(sql), params or {})
        @contextmanager
        def transaction():
            with cls.factory.begin() as session:
                yield SQLAdapter(session)
        cls.transaction = staticmethod(transaction)
        cls.modules = patch.dict(sys.modules, {'database': SimpleNamespace(Base=cls.Base, SessionLocal=cls.factory, transaction=transaction),
                                               'config': SimpleNamespace(ADMIN_IDS=[101, 102], BOT_TOKEN='12345:fixture')})
        cls.modules.start()
        cls.orders = fixture_module('models/order.py', '_test_orders')
        cls.workflow = fixture_module('models/order_workflow.py', '_test_order_workflow')
        cls.submissions = fixture_module('models/order_requirement_submission.py', '_test_submissions')
        cls.audit = fixture_module('models/admin_audit_log.py', '_test_audit')
        cls.operations = fixture_module('models/bot_operations.py', '_test_operations')
        cls.deposits = fixture_module('models/deposit.py', '_test_deposits')
        cls.users = fixture_module('models/user.py', '_test_users')
        cls.engagement = fixture_module('models/product_engagement.py', '_test_engagement')
        cls.api_keys = fixture_module('models/api_key.py', '_test_api_keys')
        cls.manager_auth = fixture_module('models/manager_auth.py', '_test_manager_auth')
        cls.promos = fixture_module('models/promocode.py', '_test_promos')
        cls.resume = fixture_module('models/bot_resume_session.py', '_test_resume')
        cls.models = patch.dict(sys.modules, {'models.order': cls.orders, 'models.order_workflow': cls.workflow,
                                              'models.order_requirement_submission': cls.submissions,
                                              'models.admin_audit_log': cls.audit,
                                              'models.bot_operations': cls.operations,
                                              'models.deposit': cls.deposits, 'models.user': cls.users,
                                              'models.product_engagement': cls.engagement, 'models.api_key': cls.api_keys,
                                              'models.manager_auth': cls.manager_auth,
                                              'models.promocode': cls.promos,
                                              'models.bot_resume_session': cls.resume,
                                              'models.product': SimpleNamespace(Product=Product),
                                              'models.provider': SimpleNamespace(Provider=Provider)})
        cls.models.start()
        cls.Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.models.stop()
        cls.modules.stop()
        cls.engine.dispose()
        cls.temp.cleanup()

    def setUp(self):
        for table in reversed(self.Base.metadata.sorted_tables):
            with self.engine.begin() as db:
                db.execute(table.delete())

    def add_order(self, **changes):
        values = dict(telegram_id=7, product_name='Test', quantity=3, automatic_quantity=2,
                      manual_pending_quantity=1, status='pending_manual')
        values.update(changes)
        with self.factory.begin() as db:
            order = self.orders.Order(**values)
            db.add(order)
            db.flush()
            return order.id

    def add_deposit(self, reference=None, **changes):
        values = dict(telegram_id=7, network='BEP20', amount=Decimal('0'), status='pending', tx_hash=reference,
                      invoice_expires_at=datetime.utcnow()+timedelta(minutes=5))
        values.update(changes)
        with self.factory.begin() as db:
            if not db.query(self.users.User).filter_by(telegram_id=7).first():
                db.add(self.users.User(id=1, telegram_id=7, full_name='Fixture', balance=Decimal('0'), total_deposited=Decimal('0')))
            deposit = self.deposits.Deposit(**values)
            db.add(deposit)
            db.flush()
            return deposit.id

    def extract_function(self, file, name, namespace):
        node = next(n for n in ast.parse((ROOT/file).read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<integration-fixture>', 'exec'), namespace)
        return namespace[name]

    def test_concurrent_confirmation_has_only_one_winner(self):
        key = action_key(1, 7, 8, 'a'*24)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: claim_checkout(key, 7, 8, 3, True), range(8)))
        self.assertEqual(sum(c['new'] for c in claims), 1)
        finish_checkout(key, {'order_id': 55})
        self.assertEqual(claim_checkout(key, 7, 8, 3, True)['order_id'], 55)

    def test_processing_or_uncertain_confirmation_is_never_replayed(self):
        key = 'a'*64
        self.assertTrue(claim_checkout(key, 7, 8, 1, True)['new'])
        self.assertFalse(claim_checkout(key, 7, 8, 1, True)['new'])
        finish_checkout(key, {'needs_review': True})
        self.assertEqual(claim_checkout(key, 7, 8, 1, True)['status'], 'uncertain')
        self.assertEqual(claim_checkout('b'*64, 7, 8, 1, False)['status'], 'expired')

    def test_committed_order_recovers_after_confirmation_status_was_not_saved(self):
        key = 'c'*64
        claim_checkout(key, 7, 8, 3, True)
        order_id = self.add_order(checkout_key=key)
        self.assertEqual(recover_checkout(key, 7), order_id)
        self.assertIsNone(recover_checkout(key, 8))

    def test_assignment_release_and_private_note_permissions(self):
        order_id = self.add_order()
        self.assertIsNone(change_work(order_id, 101, 'claim'))
        self.assertIn('Already assigned', change_work(order_id, 102, 'claim'))
        self.assertIn('Only the assigned', change_work(order_id, 102, 'release'))
        self.assertIn('configured admin', change_work(order_id, 101, 'assign', 999))
        self.assertEqual(change_work(order_id, 999, 'note', 'not permitted'), 'Access denied.')
        self.assertIsNone(change_work(order_id, 101, 'assign', 102))
        self.assertIsNone(change_work(order_id, 102, 'note', 'Waiting for restock'))
        details = work_details(order_id)
        self.assertEqual(details['assigned'], 102)
        self.assertEqual(details['remaining'], 1)
        self.assertEqual(details['notes'][0]['body'], 'Waiting for restock')
        self.assertIsNone(change_work(order_id, 102, 'release'))

    def test_queue_filters_and_priority_and_completed_orders(self):
        first = self.add_order()
        second = self.add_order(status='pending_info')
        self.add_order(status='completed')
        change_work(second, 101, 'claim')
        change_work(second, 101, 'priority')
        rows, _, total = queue_orders('all', 0, 101)
        self.assertEqual(total, 2)
        self.assertEqual(rows[0]['id'], second)
        self.assertEqual(queue_orders('ready', 0, 101)[0][0]['id'], first)
        self.assertEqual(queue_orders('mine', 0, 101)[0][0]['id'], second)
        self.assertEqual(queue_orders('unassigned', 0, 101)[2], 1)

    def test_timeline_owner_check_and_private_event_redaction(self):
        order_id = self.add_order()
        with self.transaction() as db:
            record_event(db, order_id, 'manual_pending', 'One unit awaiting delivery.')
        change_work(order_id, 101, 'note', 'Private team content')
        self.assertIsNone(read_timeline(order_id, 8))
        self.assertIsNone(read_timeline(order_id, 8, True))
        customer = read_timeline(order_id, 7)
        self.assertEqual([e['kind'] for e in customer['events']], ['created', 'manual_pending'])
        admin = read_timeline(order_id, 101, True)
        self.assertIn('note', [e['kind'] for e in admin['events']])
        self.assertNotIn('Private team content', str(admin))

    def test_closed_order_cannot_be_claimed(self):
        order_id = self.add_order(status='completed')
        self.assertIn('closed', change_work(order_id, 101, 'claim'))

    def test_checkout_key_is_unique_across_orders(self):
        from sqlalchemy.exc import IntegrityError
        self.add_order(checkout_key='d'*64)
        with self.assertRaises(IntegrityError):
            self.add_order(checkout_key='d'*64)

    def test_warranty_coverage_starts_once_after_delivery(self):
        from datetime import datetime, timedelta
        from services.warranty import start_coverage, eligibility
        order = SimpleNamespace(telegram_id=7, status='completed', refunded=False, warranty_days=7, warranty_expires_at=None)
        start_coverage(order)
        expires = order.warranty_expires_at
        self.assertGreater(expires, datetime.utcnow()+timedelta(days=6))
        start_coverage(order)
        self.assertEqual(order.warranty_expires_at, expires)
        self.assertIsNone(eligibility(order, 7))
        self.assertEqual(eligibility(order, 8), 'Order not found.')
        order.refunded = True
        self.assertIsNotNone(eligibility(order, 7))

    def test_warranty_policy_change_does_not_rewrite_existing_orders(self):
        from services.warranty import configure
        order_id = self.add_order(warranty_days=2)
        with self.factory.begin() as db:
            db.add(self.products(id=4, warranty_days=2))
        self.assertIsNone(configure(4, 30, 101))
        with self.factory() as db:
            self.assertEqual(db.get(self.products, 4).warranty_days, 30)
            self.assertEqual(db.get(self.orders.Order, order_id).warranty_days, 2)
        self.assertIsNotNone(configure(4, 5, 999))

    def test_warranty_submission_approval_delivery_and_duplicate_protection(self):
        from datetime import datetime, timedelta
        from services import warranty
        order_id = self.add_order(status='completed', warranty_days=7, warranty_expires_at=datetime.utcnow()+timedelta(days=7))
        evidence = {'kind': 'text', 'value': 'Screenshot shows that the supplied login is disabled.'}
        self.assertIsNone(warranty.submit(order_id, 7, 'The supplied account no longer signs in.', evidence))
        self.assertIsNotNone(warranty.submit(order_id, 7, 'The supplied account no longer signs in.', evidence))
        self.assertIsNone(warranty.context(order_id, 8))
        self.assertIsNone(warranty.context(order_id, 999, True))
        self.assertIsNotNone(warranty.review(order_id, 999, 'approve'))
        self.assertIsNotNone(warranty.review(order_id, 101, 'complete', 'replacement'))
        self.assertIsNone(warranty.review(order_id, 101, 'approve'))
        self.assertIsNone(warranty.review(order_id, 102, 'complete', 'Replacement details: new-account:test-value'))
        self.assertIsNotNone(warranty.review(order_id, 102, 'complete', 'duplicate'))
        with self.factory() as db:
            self.assertEqual(db.query(self.operations.WarrantyRequest).count(), 1)
            self.assertEqual(db.query(self.operations.NotificationJob).count(), 4)  # Two admins, approval, delivery.
        self.assertEqual(warranty.context(order_id, 7)['request']['status'], 'completed')

    def test_warranty_requires_eligible_order_and_evidence(self):
        from datetime import datetime, timedelta
        from services.warranty import submit
        evidence = {'kind': 'photo', 'file_id': 'fixture_photo'}
        disabled = self.add_order(status='completed')
        expired = self.add_order(status='completed', warranty_days=2, warranty_expires_at=datetime.utcnow()-timedelta(days=1))
        pending = self.add_order(warranty_days=2, warranty_expires_at=datetime.utcnow()+timedelta(days=1))
        for order_id in (disabled, expired, pending):
            self.assertIsNotNone(submit(order_id, 7, 'The account no longer works.', evidence))
        self.assertIsNotNone(submit(disabled, 7, 'x', {}))
        with self.factory() as db:
            self.assertEqual(db.query(self.operations.WarrantyRequest).count(), 0)

    def test_warranty_and_notification_roll_back_together(self):
        from datetime import datetime, timedelta
        from services.warranty import submit
        order_id = self.add_order(status='completed', warranty_days=7, warranty_expires_at=datetime.utcnow()+timedelta(days=1))
        with patch('services.notification_outbox.enqueue', side_effect=RuntimeError('test write failure')):
            with self.assertRaises(RuntimeError):
                submit(order_id, 7, 'The account is not working.', {'kind': 'photo', 'file_id': 'fixture'})
        with self.factory() as db:
            self.assertEqual(db.query(self.operations.WarrantyRequest).count(), 0)
            self.assertEqual(db.query(self.workflow.OrderEvent).count(), 0)

    def test_refund_hides_warranty_from_work_queue_but_preserves_request_history(self):
        from datetime import datetime, timedelta
        from services import warranty
        order_id = self.add_order(status='completed', warranty_days=7, warranty_expires_at=datetime.utcnow()+timedelta(days=1))
        self.assertIsNone(warranty.submit(order_id, 7, 'The account is not working.', {'kind':'photo', 'file_id':'fixture'}))
        self.assertEqual(warranty.pending()[0], 1)
        with self.factory.begin() as db:
            db.get(self.orders.Order, order_id).refunded = True
        self.assertEqual(warranty.pending()[0], 0)
        ctx = warranty.context(order_id, 101, True)
        self.assertFalse(ctx['reviewable'])
        self.assertIsNotNone(ctx['request'])
        self.assertIsNotNone(warranty.review(order_id, 101, 'approve'))

    def test_outbox_duplicate_event_and_bot_scoping(self):
        from services.notification_outbox import enqueue_once, claim_next, finish, report
        enqueue_once('order:1', 7, 'Private message', 12345)
        enqueue_once('order:1', 7, 'Private message', 12345)
        self.assertIsNone(claim_next(54321))
        job = claim_next(12345)
        self.assertIsNotNone(job)
        self.assertIsNone(claim_next(12345))
        finish(job['id'], 12345, 'sent', 99)
        counts, rows = report(12345)
        self.assertEqual(counts, {'sent': 1})
        self.assertNotIn('body', rows[0])

    def test_outbox_concurrent_consumers_claim_only_once(self):
        from services.notification_outbox import enqueue_once, claim_next
        enqueue_once('claim', 7, 'test', 12345)
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _: claim_next(12345), range(6)))
        self.assertEqual(sum(job is not None for job in jobs), 1)

    def test_uncertain_send_is_not_replayed_or_manually_retried(self):
        import asyncio
        from unittest.mock import AsyncMock
        from services.notification_outbox import enqueue_once, send_one, retry_failed, report
        enqueue_once('timeout', 7, 'test', 12345)
        bot = SimpleNamespace(id=12345, send_message=AsyncMock(side_effect=TimeoutError()))
        asyncio.run(send_one(bot))
        self.assertFalse(asyncio.run(send_one(bot)))
        counts, rows = report(12345)
        self.assertEqual(counts, {'uncertain': 1})
        self.assertIsNotNone(retry_failed(rows[0]['id'], 12345, 101))
        self.assertEqual(bot.send_message.await_count, 1)

    def test_rejected_send_can_retry_without_blocking_other_recipients(self):
        import asyncio
        from unittest.mock import AsyncMock
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import SendMessage
        from services.notification_outbox import enqueue_once, send_one, retry_failed, report
        enqueue_once('first', 7, 'test', 12345)
        enqueue_once('second', 8, 'test', 12345)
        error = TelegramForbiddenError(method=SendMessage(chat_id=7, text='test'), message='bot was blocked')
        bot = SimpleNamespace(id=12345, send_message=AsyncMock(side_effect=[error, SimpleNamespace(message_id=99)]))
        asyncio.run(send_one(bot))
        asyncio.run(send_one(bot))
        counts, rows = report(12345)
        self.assertEqual(counts, {'failed': 1, 'sent': 1})
        failed = next(row for row in rows if row['status'] == 'failed')
        self.assertIsNotNone(retry_failed(failed['id'], 12345, 999))
        self.assertIsNone(retry_failed(failed['id'], 12345, 101))
        self.assertIsNotNone(retry_failed(failed['id'], 12345, 101))  # Already queued, no duplicate retry.

    def test_rate_limit_defers_notification_without_immediate_replay(self):
        import asyncio
        from unittest.mock import AsyncMock
        from aiogram.exceptions import TelegramRetryAfter
        from aiogram.methods import SendMessage
        from services.notification_outbox import enqueue_once, send_one, report
        enqueue_once('rate-limit', 7, 'test', 12345)
        error = TelegramRetryAfter(method=SendMessage(chat_id=7, text='test'), message='retry', retry_after=17)
        bot = SimpleNamespace(id=12345, send_message=AsyncMock(side_effect=error))
        asyncio.run(send_one(bot))
        self.assertFalse(asyncio.run(send_one(bot)))
        self.assertEqual(report(12345)[0], {'queued': 1})
        self.assertEqual(bot.send_message.await_count, 1)

    def test_notification_after_telegram_success_but_db_failure_is_held(self):
        import asyncio
        from datetime import datetime, timedelta
        from unittest.mock import AsyncMock
        from services.notification_outbox import enqueue_once, send_one, recover_stale, report
        enqueue_once('interrupted', 7, 'test', 12345)
        bot = SimpleNamespace(id=12345, send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
        with patch('services.notification_outbox.finish', side_effect=RuntimeError('DB unavailable')):
            with self.assertRaises(RuntimeError):
                asyncio.run(send_one(bot))
        with self.factory.begin() as db:
            db.query(self.operations.NotificationJob).update({'claimed_at': datetime.utcnow()-timedelta(minutes=10)})
        recover_stale(12345)
        self.assertFalse(asyncio.run(send_one(bot)))
        self.assertEqual(report(12345)[0], {'uncertain': 1})
        self.assertEqual(bot.send_message.await_count, 1)

    def test_provider_health_records_sync_failure_staleness_without_secrets(self):
        from datetime import datetime, timedelta
        from services import provider_health as health
        with health._lock:
            health._pending.clear()
        error = SimpleNamespace(status_code=401, __str__=lambda: 'SECRET_VALUE')
        health.observe_request('provider-1', error)
        health.observe_sync('provider-1', error)
        health.flush_health()
        row = next(row for row in health.dashboard() if row['key'] == 'provider-1')
        self.assertEqual(row['failures'], 1)
        self.assertTrue(row['stale'])
        self.assertNotIn('SECRET_VALUE', str(row))
        health.observe_request('provider-1')
        health.observe_sync('provider-1')
        health.flush_health()
        row = next(row for row in health.dashboard() if row['key'] == 'provider-1')
        self.assertFalse(row['stale'])
        self.assertEqual(row['consecutive'], 0)
        with self.factory.begin() as db:
            db.query(self.operations.ProviderHealth).update({'last_sync_at': datetime.utcnow()-timedelta(minutes=3)})
        self.assertTrue(health.dashboard()[0]['stale'])

    def test_uncertain_resolution_does_not_fabricate_delivery_time(self):
        from services.notification_outbox import enqueue_once, claim_next, finish, resolve_uncertain, report
        enqueue_once('resolve', 7, 'test', 12345)
        job = claim_next(12345)
        finish(job['id'], 12345, 'uncertain', error_code='timeout')
        self.assertIsNotNone(resolve_uncertain(job['id'], 12345, 999))
        self.assertIsNone(resolve_uncertain(job['id'], 12345, 101))
        counts, rows = report(12345)
        self.assertEqual(counts, {'resolved': 1})
        self.assertIsNone(rows[0]['sent_at'])
        self.assertIsNone(claim_next(12345))

    def test_admin_order_timestamp_is_updated_only_after_confirmed_send(self):
        from services.notification_outbox import enqueue, claim_next, finish
        order_id = self.add_order()
        with self.transaction() as db:
            enqueue(db, 'admin-order:test', 101, 'test', 12345, source_order_id=order_id)
        job = claim_next(12345)
        with self.factory() as db:
            self.assertIsNone(db.get(self.orders.Order, order_id).admin_notification_sent_at)
        finish(job['id'], 12345, 'sent', 99)
        with self.factory() as db:
            self.assertIsNotNone(db.get(self.orders.Order, order_id).admin_notification_sent_at)

    def test_payment_reference_is_owned_immutable_and_case_normalized(self):
        from services.payment_submissions import bind_reference
        deposit_id = self.add_deposit()
        reference = '0x'+'a'*64
        self.assertEqual(bind_reference(deposit_id, reference, 8, 'BEP20'), 'not_found')
        self.assertEqual(bind_reference(deposit_id, reference, 7, 'POLYGON'), 'not_found')
        self.assertEqual(bind_reference(deposit_id, reference.upper(), 7, 'BEP20'), 'ok')
        self.assertEqual(bind_reference(deposit_id, reference, 7, 'BEP20'), 'already_submitted')
        self.assertEqual(bind_reference(deposit_id, '0x'+'b'*64, 7, 'BEP20'), 'reference_locked')
        second = self.add_deposit()
        self.assertEqual(bind_reference(second, reference, 7, 'BEP20'), 'duplicate')
        with self.factory() as db:
            self.assertEqual(db.get(self.deposits.Deposit, deposit_id).tx_hash, reference)

    def test_competing_references_cannot_replace_the_winning_submission(self):
        from services.payment_submissions import bind_reference
        deposit_id = self.add_deposit()
        gate = threading.Barrier(2)
        def bind(char):
            gate.wait()
            return bind_reference(deposit_id, '0x'+char*64, 7, 'BEP20')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(bind, ('a', 'b')))
        self.assertEqual(sorted(results), ['ok', 'reference_locked'])

    def test_completed_payment_cannot_be_expired_by_a_stale_submission(self):
        from services.payment_submissions import bind_reference
        deposit_id = self.add_deposit('0x'+'a'*64, status='completed', invoice_expires_at=datetime.utcnow()-timedelta(minutes=1))
        self.assertEqual(bind_reference(deposit_id, '0x'+'b'*64, 7, 'BEP20'), 'completed')
        with self.factory() as db:
            row = db.get(self.deposits.Deposit, deposit_id)
            self.assertEqual(row.status, 'completed')
            self.assertEqual(row.tx_hash, '0x'+'a'*64)

    def test_parallel_verification_credits_one_reference_once(self):
        from services.payment_submissions import credit_verified
        deposit_id = self.add_deposit('0x'+'a'*64)
        gate = threading.Barrier(3)
        def credit(_):
            with self.factory() as db:
                row = db.get(self.deposits.Deposit, deposit_id)
                gate.wait()
                return credit_verified(db, row, Decimal('1.23456789'))
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(credit, range(3)))
        self.assertTrue(all(results))
        with self.factory() as db:
            user = db.query(self.users.User).filter_by(telegram_id=7).one()
            self.assertEqual(user.balance, Decimal('1.23456789'))
            self.assertEqual(user.total_deposited, Decimal('1.23456789'))
            self.assertEqual(db.get(self.deposits.Deposit, deposit_id).status, 'completed')

    def test_two_verified_deposits_do_not_lose_a_wallet_increment(self):
        from services.payment_submissions import credit_verified
        ids = [self.add_deposit('0x'+char*64) for char in ('a', 'b')]
        gate = threading.Barrier(2)
        def credit(deposit_id):
            with self.factory() as db:
                row = db.get(self.deposits.Deposit, deposit_id)
                gate.wait()
                return credit_verified(db, row, Decimal('1.25'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertTrue(all(pool.map(credit, ids)))
        with self.factory() as db:
            user = db.query(self.users.User).filter_by(telegram_id=7).one()
            self.assertEqual(user.balance, Decimal('2.50'))
            self.assertEqual(user.total_deposited, Decimal('2.50'))

    def test_invalid_credit_or_closed_invoice_leaves_wallet_unchanged(self):
        from services.payment_submissions import credit_verified
        deposit_id = self.add_deposit('0x'+'a'*64)
        for amount in ('NaN', 'Infinity', '-1', '0'):
            with self.factory() as db:
                self.assertFalse(credit_verified(db, db.get(self.deposits.Deposit, deposit_id), amount))
        with self.factory.begin() as db:
            db.get(self.deposits.Deposit, deposit_id).status = 'failed'
        with self.factory() as db:
            self.assertFalse(credit_verified(db, db.get(self.deposits.Deposit, deposit_id), '1'))
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('0'))

    def test_restock_consumption_and_notification_commit_together(self):
        row_model = self.engagement.ProductEngagement
        with self.factory.begin() as db:
            db.add(row_model(telegram_id=7, product_id=21, restock_enabled=True, is_favorite=True))
        queue = self.extract_function('handlers/products.py', '_queue_restock_subscription', {'ProductEngagement':row_model})
        with patch('services.notification_outbox.enqueue', side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):
                queue(7, 21, 12345, 'Back in stock', None)
        with self.factory() as db:
            self.assertTrue(db.query(row_model).one().restock_enabled)
        self.assertTrue(queue(7, 21, 12345, 'Back in stock', None))
        self.assertFalse(queue(7, 21, 12345, 'Back in stock', None))
        with self.factory() as db:
            self.assertEqual(db.query(self.operations.NotificationJob).count(), 1)

    def test_notification_preserves_keyboard_and_skips_blocked_destination(self):
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import SendMessage
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from unittest.mock import AsyncMock
        from services.notification_outbox import enqueue_once, send_one, report
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='View', callback_data='product_21')]])
        enqueue_once('first', 7, 'test', 12345, markup)
        enqueue_once('second', 7, 'test', 12345, markup)
        enqueue_once('other', 8, 'test', 12345, markup)
        error = TelegramForbiddenError(method=SendMessage(chat_id=7, text='test'), message='blocked')
        bot = SimpleNamespace(id=12345, send_message=AsyncMock(side_effect=[error,SimpleNamespace(message_id=9)]))
        asyncio.run(send_one(bot))
        asyncio.run(send_one(bot))
        self.assertFalse(asyncio.run(send_one(bot)))
        self.assertEqual(report(12345)[0], {'failed':1, 'queued':1, 'sent':1})
        self.assertEqual(bot.send_message.await_args.args[0], '8')
        self.assertEqual(bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data, 'product_21')

    def test_api_retry_recovers_committed_order_without_a_second_purchase(self):
        from sqlalchemy.exc import IntegrityError
        from services.checkout_actions import api_checkout_key
        with self.factory.begin() as db:
            db.add(self.api_keys.ApiKey(id=21, telegram_id=7, key_prefix='fixture', key_hash='a'*64, is_active=True))
        namespace = dict(SessionLocal=self.factory, ApiOrder=self.api_keys.ApiOrder, IntegrityError=IntegrityError)
        reserve = self.extract_function('api/reseller_v1.py', '_reserve_order', namespace)
        save_result = self.extract_function('api/reseller_v1.py', '_set_api_order_result', namespace)
        request, new = reserve(21, 'client-1')
        self.assertTrue(new)
        key = api_checkout_key(21, 'client-1')
        order_id = self.add_order(checkout_key=key)
        retry, new = reserve(21, 'client-1')
        self.assertFalse(new)
        recovered = recover_checkout(key, 7)
        self.assertEqual(recovered, order_id)
        save_result(retry.id, recovered)
        last, new = reserve(21, 'client-1')
        self.assertFalse(new)
        self.assertEqual(last.order_id, order_id)
        self.assertNotEqual(key, api_checkout_key(22, 'client-1'))
        with self.factory() as db:
            self.assertEqual(db.query(self.orders.Order).count(), 1)

    def test_stale_verification_does_not_credit_a_changed_reference(self):
        from services.payment_submissions import credit_verified
        deposit_id = self.add_deposit('0x'+'a'*64)
        with self.factory() as stale:
            original = stale.get(self.deposits.Deposit, deposit_id)
            with self.factory.begin() as db:
                db.get(self.deposits.Deposit, deposit_id).tx_hash = '0x'+'b'*64
            self.assertFalse(credit_verified(stale, original, '1'))
        with self.factory() as db:
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('0'))

    def test_failed_verification_cannot_demote_completed_payment(self):
        from unittest.mock import Mock
        deposit_id = self.add_deposit('0x'+'a'*64, status='completed')
        finalize = self.extract_function('services/deposit_checker.py', '_finalize_failed',
              {'Deposit':self.deposits.Deposit, 'DELETE_FAILED_DEPOSITS':True, 'logger':Mock(), '_clear_pending_attempts':Mock()})
        with self.factory() as db:
            finalize(db, db.get(self.deposits.Deposit, deposit_id), 'late failure')
        with self.factory() as db:
            self.assertEqual(db.get(self.deposits.Deposit, deposit_id).status, 'completed')

    def test_manager_actor_is_read_from_the_verified_session(self):
        import hashlib
        from unittest.mock import Mock
        token = 'fixture-browser-session'
        with self.factory.begin() as db:
            db.add(self.manager_auth.ManagerAuthSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                   telegram_id=101, client_ip='127.0.0.1', created_at=datetime.utcnow(), expires_at=datetime.utcnow()+timedelta(hours=1)))
        validate = Mock(return_value=True)
        actor = self.extract_function('services/manager_auth.py', 'session_owner', dict(valid_session=validate,
                SessionLocal=self.factory, ManagerAuthSession=self.manager_auth.ManagerAuthSession,
                _hash=lambda value: hashlib.sha256(value.encode()).hexdigest(), _now=datetime.utcnow))
        self.assertEqual(actor(token, '127.0.0.1'), 101)
        validate.assert_called_once_with(token, '127.0.0.1')
        validate.return_value = False
        self.assertIsNone(actor(token, 'changed-ip'))

    def test_supplier_callback_commits_completion_and_one_notification_together(self):
        import hashlib, hmac, json, time
        from html import escape
        from unittest.mock import AsyncMock
        from services.notification_outbox import enqueue
        from services.warranty import start_coverage
        with self.factory.begin() as db:
            db.add(self.providers(id=21, name='Fixture supplier', api_key='fixture-key', api_secret='fixture-secret', is_active=True))
        order_id = self.add_order(status='processing', reseller_id=21, reseller_order_id='upstream-1', warranty_days=7)
        namespace = dict(SessionLocal=self.factory, Provider=self.providers, User=self.users.User, Order=self.orders.Order,
                         time=time, hashlib=hashlib, hmac=hmac, json=json, escape=escape, start_coverage=start_coverage,
                         record_event=record_event, enqueue=enqueue, _callback_error=lambda code: {'ok':False,'error':code})
        node = next(n for n in ast.parse((ROOT/'services/upstream_callbacks.py').read_text(encoding='utf-8')).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_upstream_callback')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<supplier-fixture>', 'exec'), namespace)
        raw = json.dumps({'downstream_order_no':'upstream-1','status':'completed','fulfillment':{'payload':'fixture-account:fixture-value'}}).encode()
        stamp = str(int(time.time()))
        signing = f'POST\n/api/v1/upstream/callback\n{stamp}\n{hashlib.md5(raw).hexdigest()}'
        headers = {'Dujiao-Next-Api-Key':'fixture-key', 'Dujiao-Next-Timestamp':stamp,
                   'Dujiao-Next-Signature':hmac.new(b'fixture-secret', signing.encode(), hashlib.sha256).hexdigest()}
        callback = namespace['process_upstream_callback']
        bot = SimpleNamespace(send_message=AsyncMock())
        self.assertTrue(asyncio.run(callback(raw, headers, bot))['ok'])
        self.assertEqual(asyncio.run(callback(raw, headers, bot))['message'], 'already_processed')
        with self.factory() as db:
            self.assertEqual(db.get(self.orders.Order, order_id).status, 'completed')
            self.assertIsNotNone(db.get(self.orders.Order, order_id).warranty_expires_at)
            self.assertEqual(db.query(self.operations.NotificationJob).count(), 1)
        bot.send_message.assert_not_awaited()

    def test_timeline_keeps_event_order_when_timestamps_are_equal(self):
        order_id = self.add_order()
        with self.transaction() as db:
            for kind in ('created', 'information', 'completed'):
                record_event(db, order_id, kind, kind)
        with self.factory.begin() as db:
            db.query(self.workflow.OrderEvent).update({'created_at':datetime(2026,11,6,12,0)})
        self.assertEqual([event['kind'] for event in read_timeline(order_id, 7)['events']],
                         ['created','information','completed'])

    def test_promo_redeems_for_multiple_users_but_once_per_user(self):
        from services.promotions import redeem
        with self.factory.begin() as db:
            db.add(self.promos.PromoCode(code='FIXTURE', amount=1.25, max_uses=2, used_count=0, is_active=True, created_by=101))
            for user_id in (7,8):
                db.add(self.users.User(id=user_id, telegram_id=user_id, full_name='Fixture', balance=Decimal('0')))
        self.assertTrue(redeem(7, 'fixture')[0])
        self.assertFalse(redeem(7, 'FIXTURE')[0])
        self.assertTrue(redeem(8, 'FIXTURE')[0])
        with self.factory() as db:
            self.assertEqual(db.query(self.promos.PromoCode).one().used_count, 2)
            self.assertEqual(db.query(self.deposits.Deposit).count(), 2)
            self.assertEqual({u.balance for u in db.query(self.users.User).all()}, {Decimal('1.25')})

    def test_promo_global_limit_is_not_exceeded_by_parallel_redemption(self):
        from services.promotions import redeem
        with self.factory.begin() as db:
            db.add(self.promos.PromoCode(code='ONLYONE', amount=1, max_uses=1, used_count=0, is_active=True, created_by=101))
            for user_id in (7,8):
                db.add(self.users.User(id=user_id, telegram_id=user_id, full_name='Fixture', balance=Decimal('0')))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda uid: redeem(uid, 'ONLYONE'), (7,8)))
        self.assertEqual(sum(ok for ok, _ in results), 1)
        with self.factory() as db:
            self.assertEqual(db.query(self.promos.PromoCode).one().used_count, 1)
            self.assertEqual(sum(u.balance for u in db.query(self.users.User).all()), Decimal('1'))

    def test_legacy_promo_receipt_still_blocks_replay(self):
        from services.promotions import redeem
        with self.factory.begin() as db:
            db.add(self.promos.PromoCode(code='LEGACY', amount=1, max_uses=20, used_count=1, is_active=True, created_by=101))
        self.add_deposit('PROMO_LEGACY', network='PROMO', status='completed')
        self.assertFalse(redeem(7, 'LEGACY')[0])
        with self.factory() as db:
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('0'))

    def refund_order(self, **changes):
        values = dict(status='completed', aftercare_policy='refund', subscription_days=30,
                      coverage_started_at=datetime.utcnow()-timedelta(days=2, hours=1), amount=Decimal('100'))
        values.update(changes)
        with self.factory.begin() as db:
            if not db.query(self.users.User).filter_by(telegram_id=7).first():
                db.add(self.users.User(id=7, telegram_id=7, full_name='Fixture', balance=Decimal('5')))
        return self.add_order(**values)

    def test_refund_requires_deactivation_time_and_screenshot(self):
        from services import warranty
        order_id = self.refund_order()
        reason = 'The supplied account was deactivated.'
        self.assertIsNotNone(warranty.submit(order_id, 7, reason, {'kind':'text','value':'The login fails with an error.'}, 'Today'))
        self.assertIsNotNone(warranty.submit(order_id, 7, reason, {'kind':'photo','file_id':'fixture'}))
        self.assertIsNotNone(warranty.submit(order_id, 8, reason, {'kind':'photo','file_id':'fixture'}, 'Today'))
        self.assertIsNone(warranty.submit(order_id, 7, reason, {'kind':'photo','file_id':'fixture'}, '6 Nov 2026, 2 PM IST'))
        self.assertIsNotNone(warranty.submit(order_id, 7, reason, {'kind':'photo','file_id':'fixture'}, 'Today'))
        ctx = warranty.context(order_id, 7)
        self.assertEqual(ctx['request']['refund_amount'], Decimal('72'))
        self.assertEqual(ctx['request']['used_days'], 3)
        self.assertIn('2 PM IST', warranty.request_text(ctx))

    def test_refund_approval_credits_saved_calculation_once_and_closes_order(self):
        from services import warranty
        order_id = self.refund_order()
        self.assertIsNone(warranty.submit(order_id, 7, 'The account was deactivated.', {'kind':'photo','file_id':'fixture'}, 'Unknown'))
        self.assertIsNotNone(warranty.review(order_id, 999, 'approve'))
        self.assertIsNotNone(warranty.review(order_id, 101, 'complete', 'Not an approval'))
        with self.factory.begin() as db:
            # Approval delay must not reduce the amount already requested.
            db.get(self.orders.Order, order_id).coverage_started_at -= timedelta(days=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda admin: warranty.review(order_id, admin, 'approve'), (101,102)))
        self.assertEqual(sum(result is None for result in results), 1)
        with self.factory() as db:
            order = db.get(self.orders.Order, order_id)
            self.assertTrue(order.refunded)
            self.assertEqual(order.status, 'refunded')
            self.assertEqual(order.refund_amount, Decimal('72'))
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('77'))
            self.assertEqual(db.query(self.audit.AdminAuditLog).filter_by(action='policy_refund').count(), 1)
            self.assertEqual(db.query(self.operations.NotificationJob).count(), 3)
        self.assertEqual(warranty.pending()[0], 0)
        self.assertEqual(warranty.context(order_id, 7)['request']['status'], 'completed')

    def test_refund_failure_rolls_back_wallet_order_request_and_notifications(self):
        from services import warranty
        order_id = self.refund_order()
        self.assertIsNone(warranty.submit(order_id, 7, 'The account was deactivated.', {'kind':'photo','file_id':'fixture'}, 'Today'))
        with patch('services.notification_outbox.enqueue', side_effect=RuntimeError('fixture failed write')):
            with self.assertRaises(RuntimeError):
                warranty.review(order_id, 101, 'approve')
        with self.factory() as db:
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('5'))
            self.assertFalse(db.get(self.orders.Order, order_id).refunded)
            self.assertEqual(db.query(self.operations.WarrantyRequest).one().status, 'submitted')
            self.assertEqual(db.query(self.audit.AdminAuditLog).count(), 0)

    def test_refund_decline_does_not_credit_or_change_delivery(self):
        from services import warranty
        order_id = self.refund_order(delivered_account='Already used credentials')
        self.assertIsNone(warranty.submit(order_id, 7, 'The account was deactivated.', {'kind':'photo','file_id':'fixture'}, 'Today'))
        self.assertIsNone(warranty.review(order_id, 101, 'reject', 'Evidence does not match this order.'))
        with self.factory() as db:
            self.assertEqual(db.query(self.users.User).one().balance, Decimal('5'))
            self.assertEqual(db.get(self.orders.Order, order_id).delivered_account, 'Already used credentials')
            self.assertFalse(db.get(self.orders.Order, order_id).refunded)
        self.assertIsNotNone(warranty.review(order_id, 101, 'approve'))

    def test_changed_quote_requires_customer_to_review_again(self):
        from services import warranty
        order_id = self.refund_order()
        error = warranty.submit(order_id, 7, 'The account was deactivated.', {'kind':'photo','file_id':'fixture'}, 'Today', '100.00000000')
        self.assertIn('estimate changed', error)
        with self.factory() as db:
            self.assertEqual(db.query(self.operations.WarrantyRequest).count(), 0)

    def test_product_policy_choice_and_snapshot_survive_later_edits(self):
        from services import warranty
        with self.factory.begin() as db:
            db.add(self.products(id=4, warranty_days=0))
        self.assertIsNone(warranty.configure_policy(4, 'refund', 30, 101))
        with self.factory() as db:
            snapshot = warranty.policy_snapshot(db.get(self.products, 4))
        order_id = self.refund_order(**snapshot)
        self.assertIsNone(warranty.configure_policy(4, 'skip', 0, 101))
        with self.factory() as db:
            self.assertEqual(warranty.policy(db.get(self.products,4)), 'skip')
            self.assertEqual(warranty.policy(db.get(self.orders.Order, order_id)), 'refund')
            self.assertEqual(db.get(self.orders.Order, order_id).subscription_days, 30)
        self.assertIsNotNone(warranty.configure_policy(4, 'refund', 0, 101))
        self.assertIsNotNone(warranty.configure_policy(4, 'refund', 30, 999))

    def test_admin_resume_draft_is_encrypted_and_bound_to_user_and_chat(self):
        from services.session_resume import make_snapshot, _persist, _load, open_snapshot
        snapshot = make_snapshot('AddProduct:accounts',dict(name='Fixture', draft_accounts=['fixture:user'],
                                 price=Decimal('1.25'), password='not retained'),admin=True,
                                 prompt={'text':'Upload accounts','reply_markup':None})
        key = (101,101)
        _persist(key,snapshot)
        with self.factory() as db:
            stored = db.get(self.resume.BotResumeSession,key).snapshot
        self.assertNotIn('fixture:user',str(stored))
        self.assertNotIn('Upload accounts',str(stored))
        self.assertEqual(_load(key),snapshot)
        self.assertIsNone(open_snapshot((102,101),stored))
        tampered = {**stored,'ciphertext':'x'+stored['ciphertext'][1:]}
        self.assertIsNone(open_snapshot(key,tampered))
        self.assertNotIn('password',snapshot['data'])

    def test_invoice_is_one_hour_and_switching_network_reuses_original_expiry(self):
        from unittest.mock import Mock
        with self.factory.begin() as db:
            db.add(self.users.User(id=7,telegram_id=7,full_name='Fixture'))
        create = self.extract_function('handlers/deposit.py','_create_invoice',dict(
            SessionLocal=self.factory,User=self.users.User,Deposit=self.deposits.Deposit,
            Decimal=Decimal,datetime=datetime,timedelta=timedelta,INVOICE_LIFETIME_SECONDS=3600,
            secrets=SimpleNamespace(randbelow=Mock(side_effect=[0,1,2])),_submitted_invoice_from_db=lambda *args:None))
        before = datetime.utcnow()
        first = create(7,Decimal('0'),'BEP20')
        self.assertLess(abs((datetime.fromisoformat(first['expires_at'])-before).total_seconds()-3600),3)
        second = create(7,Decimal('0'),'POLYGON')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(first['expires_at'],second['expires_at'])
        with self.factory.begin() as db:
            db.get(self.deposits.Deposit,first['id']).invoice_expires_at = datetime.utcnow()-timedelta(seconds=1)
        third = create(7,Decimal('0'),'BINANCE')
        self.assertNotEqual(first['id'],third['id'])
        with self.factory() as db:
            self.assertEqual(db.query(self.deposits.Deposit).count(),2)
