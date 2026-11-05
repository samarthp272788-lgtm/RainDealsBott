"""Bind one payment reference to its owner's invoice without replacing prior work."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError


def bind_reference(deposit_id, reference, user_id, network):
    from database import transaction
    from models.deposit import Deposit
    reference = reference.strip()
    if network in ('BEP20', 'POLYGON'):
        reference = reference.lower()
    try:
        with transaction() as db:
            deposit = db.query(Deposit).filter(Deposit.id == deposit_id).with_for_update().first()
            if not deposit or deposit.telegram_id != user_id or deposit.network != network:
                return 'not_found'
            if deposit.status == 'completed':
                return 'completed'
            if deposit.status in ('failed', 'expired', 'cancelled'):
                return 'closed'
            if deposit.tx_hash:
                # A repeat is acknowledged; a different reference needs another invoice.
                return 'already_submitted' if deposit.tx_hash.casefold() == reference.casefold() else 'reference_locked'
            if deposit.invoice_expires_at and deposit.invoice_expires_at <= datetime.utcnow():
                deposit.status = 'expired'
                return 'expired'
            bound = db.query(Deposit).filter(Deposit.id == deposit_id, Deposit.tx_hash.is_(None),
                      Deposit.status == deposit.status).update({Deposit.tx_hash: reference, Deposit.status: 'pending',
                          Deposit.verification_started_at: datetime.utcnow()}, synchronize_session=False)
            if bound != 1:
                fresh = db.query(Deposit).filter(Deposit.id == deposit_id).populate_existing().with_for_update().first()
                if fresh.status == 'completed':
                    return 'completed'
                if fresh.status in ('failed', 'expired', 'cancelled'):
                    return 'closed'
                return 'already_submitted' if fresh.tx_hash and fresh.tx_hash.casefold() == reference.casefold() else 'reference_locked'
        return 'ok'
    except IntegrityError:
        from database import SessionLocal
        with SessionLocal() as db:
            if db.query(Deposit.id).filter(Deposit.tx_hash == reference).first():
                return 'duplicate'
        raise


def credit_verified(db, original, amount, inr_amount=None, conversion_rate=None):
    """Final accounting only: callers must already have verified the actual payment."""
    from models.deposit import Deposit
    from models.user import User
    expected = (original.tx_hash, original.network, original.telegram_id)
    try:
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount <= 0:
            db.rollback()
            return False
        extras = {name: Decimal(str(value)) for name, value in
                  (('inr_amount', inr_amount), ('conversion_rate', conversion_rate)) if value is not None}
        if any(not value.is_finite() or value <= 0 for value in extras.values()):
            db.rollback()
            return False
    except (InvalidOperation, ValueError):
        db.rollback()
        return False
    deposit = db.query(Deposit).filter(Deposit.id == original.id).populate_existing().with_for_update().first()
    if not deposit or (deposit.tx_hash, deposit.network, deposit.telegram_id) != expected:
        db.rollback()
        return False
    if deposit.status == 'completed':
        db.rollback()
        return True
    if deposit.status in ('failed', 'expired', 'cancelled') or not deposit.tx_hash:
        db.rollback()
        return False
    duplicate = db.query(Deposit.id).filter(Deposit.tx_hash == deposit.tx_hash,
                    Deposit.status == 'completed', Deposit.id != deposit.id).first()
    if duplicate:
        db.rollback()
        return False
    # Conditional transition is a second guard even on databases without FOR UPDATE.
    claimed = db.query(Deposit).filter(Deposit.id == deposit.id, Deposit.status == deposit.status,
                Deposit.tx_hash == expected[0]).update({Deposit.status: 'crediting'}, synchronize_session=False)
    if claimed != 1:
        deposit_id = deposit.id
        db.rollback()
        fresh = db.get(Deposit, deposit_id)
        return bool(fresh and fresh.status == 'completed' and (fresh.tx_hash, fresh.network, fresh.telegram_id) == expected)
    # Increment at the database: two different deposits cannot lose a wallet update.
    credited = db.query(User).filter(User.telegram_id == deposit.telegram_id).update({
        User.balance: func.coalesce(User.balance, 0) + amount,
        User.total_deposited: func.coalesce(User.total_deposited, 0) + amount,
    }, synchronize_session=False)
    if credited != 1:
        db.rollback()
        return False
    deposit.amount = deposit.received_amount = amount
    deposit.status = 'completed'
    for name, value in extras.items():
        setattr(deposit, name, value)
    db.commit()
    return True
