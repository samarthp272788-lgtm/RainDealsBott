"""Transactional wallet history shared by money writers."""
from decimal import Decimal
import secrets

def record_wallet(db, user, label, before):
    """Called by wallet writers, in the same transaction as the money change."""
    from models.customer_tools import WalletEntry
    before, after = Decimal(str(before or 0)), Decimal(str(user.balance or 0))
    with db.no_autoflush:
        pending = [r for r in db.new if isinstance(r,WalletEntry) and r.telegram_id==user.telegram_id]
        if pending and Decimal(str(pending[-1].balance))==after:
            return
        latest = pending[-1] if pending else db.query(WalletEntry).filter_by(telegram_id=user.telegram_id).order_by(WalletEntry.id.desc()).with_for_update().first()
        prior = Decimal(str(latest.balance)) if latest else Decimal(0)
        if latest is None or prior != before:
            db.add(WalletEntry(telegram_id=user.telegram_id, event_key=secrets.token_hex(24),
                               label='Opening balance' if latest is None else 'External wallet adjustment / reconciliation',
                               amount=before-prior, balance=before))
        if after != before:
            db.add(WalletEntry(telegram_id=user.telegram_id, event_key=secrets.token_hex(24), label=label[:200],
                               amount=after-before, balance=after))

