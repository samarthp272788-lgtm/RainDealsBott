"""Refund arithmetic uses synthetic paid amounts and delivery timestamps only."""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from services.warranty import refund_quote, eligibility, start_coverage, policy


class RefundPolicyTests(unittest.TestCase):
    def order(self, **changes):
        values = dict(aftercare_policy='refund', subscription_days=30, warranty_days=0,
                      coverage_started_at=datetime(2026, 11, 1), warranty_expires_at=None,
                      amount=Decimal('30'), telegram_id=7, status='completed', refunded=False)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_exact_rate_boundaries_and_remaining_days(self):
        order = self.order()
        for hours, used, amount in ((0,1,'30'), (23,1,'30'), (24,2,'30'), (47,2,'30'),
                                    (48,3,'21.6'), (167,7,'18.4'), (168,8,'16.5')):
            quote = refund_quote(order, order.coverage_started_at+timedelta(hours=hours))
            self.assertEqual(quote['used_days'], used)
            self.assertEqual(quote['remaining_days'], 30-used)
            self.assertEqual(quote['refund_amount'], Decimal(amount))

    def test_uses_total_actually_paid_and_rounds_down_to_wallet_precision(self):
        order = self.order(amount=Decimal('2.12345678'))
        quote = refund_quote(order, order.coverage_started_at+timedelta(days=3))
        self.assertEqual(quote['refund_amount'], Decimal('1.47226336'))
        self.assertLessEqual(quote['refund_amount'], order.amount)

    def test_skip_missing_start_expired_and_future_delivery_cannot_claim(self):
        for changes in ({'aftercare_policy':'skip'}, {'coverage_started_at':None}, {'subscription_days':0},
                        {'coverage_started_at':datetime(2027,1,1)}, {'refunded':True}, {'status':'pending_manual'}):
            self.assertIsNotNone(eligibility(self.order(**changes),7,datetime(2026,11,5)))
        self.assertIsNotNone(eligibility(self.order(), 8, datetime(2026,11,5)))
        self.assertIsNotNone(eligibility(self.order(), 7, datetime(2026,12,1)))
        self.assertIsNone(eligibility(self.order(), 7, datetime(2026,11,5)))

    def test_delivery_clock_is_idempotent_for_refund_and_preserves_legacy_warranty(self):
        order = self.order(coverage_started_at=None)
        start_coverage(order)
        first = order.coverage_started_at
        start_coverage(order)
        self.assertEqual(order.coverage_started_at, first)
        legacy = self.order(aftercare_policy=None,warranty_days=7,warranty_expires_at=datetime(2026,11,8),coverage_started_at=None)
        self.assertEqual(policy(legacy), 'warranty')
        start_coverage(legacy)
        self.assertEqual(legacy.coverage_started_at, datetime(2026,11,1))
        self.assertEqual(legacy.warranty_expires_at, datetime(2026,11,8))
