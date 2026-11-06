"""One redemption transaction shared by the wallet and /redeem entry points."""
from decimal import Decimal
import re
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError


def redeem(user_id, code):
    from database import transaction
    from models.promocode import PromoCode
    from models.user import User
    from models.deposit import Deposit
    code = str(code or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9_-]{1,32}', code):
        return False, '❌ Invalid promocode.'
    reference = f'PROMO_{code}_{user_id}'
    try:
        with transaction() as db:
            promo = db.query(PromoCode).filter(PromoCode.code == code).with_for_update().first()
            if not promo or not promo.is_active:
                return False, '❌ This promocode is unavailable.'
            limit, used = promo.max_uses, int(promo.used_count or 0)
            if limit is None or limit < 0 or (limit and used >= limit):
                return False, '❌ This promocode has reached its usage limit.'
            user = db.query(User).filter(User.telegram_id == user_id).with_for_update().first()
            if not user or user.is_banned:
                return False, '❌ User unavailable. Please use /start or contact support.'
            previous = db.query(Deposit.id).filter(Deposit.telegram_id == user_id,
                 Deposit.tx_hash.in_((f'PROMO_{code}', reference)), Deposit.status == 'completed').first()
            if previous:
                return False, '❌ You have already redeemed this promocode.'
            amount = Decimal(str(promo.amount))
            if not amount.is_finite() or amount <= 0:
                return False, '❌ This promocode is unavailable.'
            claimed = db.query(PromoCode).filter(PromoCode.id == promo.id, func.coalesce(PromoCode.used_count,0) == used,
                 PromoCode.is_active == True).update({PromoCode.used_count: used+1}, synchronize_session=False)
            if claimed != 1:
                return False, 'The promocode changed while processing. Please try again.'
            before_balance = Decimal(str(user.balance or 0))
            credited = db.query(User).filter(User.telegram_id == user_id).update(
                 {User.balance: func.coalesce(User.balance, 0)+amount}, synchronize_session=False)
            if credited != 1:
                raise RuntimeError('The wallet changed during redemption; transaction rolled back')
            db.add(Deposit(telegram_id=user_id, amount=amount, tx_hash=reference, network='PROMO', status='completed'))
            db.flush()
            db.refresh(user)
            from services.wallet_ledger import record_wallet
            record_wallet(db,user,f"Promocode {code}",before_balance)
            result = dict(code=code, amount=amount, balance=user.balance)
        return True, result
    except IntegrityError:
        return False, '❌ This redemption was already recorded. Check your wallet before retrying.'
