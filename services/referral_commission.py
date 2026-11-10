"""Reverse only referral commission actually recorded on the source order."""
from decimal import Decimal, ROUND_DOWN


def reverse_saved_commission(db, order, referrer_id, refund_amount, admin_id, invoice_snapshot=None):
    from models.user import User
    from models.admin_audit_log import AdminAuditLog
    from services.wallet_ledger import record_wallet

    saved_value = getattr(order, 'referral_commission_amount', None)
    if saved_value is None:
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id), action='refund_legacy_commission_review',
            entity_type='order', entity_id=str(order.id),
            summary='No commission amount was recorded on this legacy order; no current-rate estimate was reversed.',
        ))
        return {'status': 'legacy_unrecorded', 'amount': Decimal('0')}

    original = Decimal(str(saved_value or 0))
    paid = Decimal(str(order.amount or 0))
    refund = Decimal(str(refund_amount or 0))
    if not original.is_finite() or original < 0 or not paid.is_finite() or paid <= 0 or not refund.is_finite() or refund < 0:
        raise RuntimeError('Saved referral commission data is invalid; refund rolled back.')
    amount = (original * min(Decimal('1'), refund / paid)).quantize(Decimal('.00000001'), rounding=ROUND_DOWN)
    if amount <= 0 or not referrer_id:
        return {'status': 'none', 'amount': Decimal('0')}
    referrer = db.query(User).filter(User.telegram_id == referrer_id).with_for_update().first()
    if not referrer:
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id), action='refund_referrer_missing',
            entity_type='order', entity_id=str(order.id),
            summary=f'Saved commission {amount:.8f} could not be reversed because the referrer row is missing.',
        ))
        return {'status': 'referrer_missing', 'amount': amount}

    earnings = Decimal(str(referrer.referral_earnings or 0))
    earnings_debit = min(max(Decimal('0'), earnings), amount)
    referrer.referral_earnings = earnings - earnings_debit

    credited = getattr(order, 'referral_commission_to_balance', None)
    if credited is None:
        credited = (invoice_snapshot or {}).get('referral_credit_to_balance')
    balance_debit = Decimal('0')
    if credited is True:
        before = Decimal(str(referrer.balance or 0))
        balance_debit = min(max(Decimal('0'), before), amount)
        referrer.balance = before - balance_debit
        if balance_debit:
            record_wallet(db, referrer, f'Referral commission reversal · order #{order.id}', before)

    if earnings_debit < amount or (credited is True and balance_debit < amount) or credited is None:
        db.add(AdminAuditLog(
            admin_telegram_id=int(admin_id), action='refund_commission_shortfall',
            entity_type='order', entity_id=str(order.id),
            summary=(f'Commission target {amount:.8f}; earnings reversed {earnings_debit:.8f}; '
                     f'wallet reversed {balance_debit:.8f}; historical payout setting '
                     f'{"unknown" if credited is None else "saved"}.'),
        ))
    return {'status': 'reversed', 'amount': amount, 'earnings_debit': earnings_debit,
            'balance_debit': balance_debit}
