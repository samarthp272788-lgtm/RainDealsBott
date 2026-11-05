"""Durable claims for one specific confirmation, never a per-user rate limit."""
import hashlib
import secrets


def action_key(bot_id, user_id, product_id, token):
    return hashlib.sha256(f'{bot_id}:{user_id}:{product_id}:{token}'.encode()).hexdigest()


def api_checkout_key(api_key_id, client_order_id):
    return hashlib.sha256(f'api:{api_key_id}:{client_order_id}'.encode()).hexdigest()


async def confirmation_token(state, product_id, reuse=False):
    key = f'checkout_token_{product_id}'
    data = await state.get_data()
    token = data.get(key) if reuse else None
    token = token or secrets.token_hex(12)
    await state.update_data(**{key: token})
    return token


def claim_checkout(key, user_id, product_id, quantity, allow_create):
    from sqlalchemy import text
    from database import transaction
    with transaction() as db:
        prior = db.execute(text('SELECT status,order_id FROM bot_checkout_attempts WHERE action_key=:key'), {'key': key}).mappings().first()
        if prior:
            return {'new': False, **dict(prior)}
        if not allow_create:
            return {'new': False, 'status': 'expired', 'order_id': None}
        inserted = db.execute(text(
            "INSERT IGNORE INTO bot_checkout_attempts (action_key,telegram_id,product_id,quantity,status,created_at,updated_at) "
            "VALUES (:key,:user,:product,:quantity,'processing',UTC_TIMESTAMP(),UTC_TIMESTAMP())"
        ), {'key': key, 'user': user_id, 'product': product_id, 'quantity': quantity}).rowcount
        if inserted == 1:
            return {'new': True, 'status': 'processing', 'order_id': None}
        # A current read sees the winning insert even under REPEATABLE READ.
        prior = db.execute(text('SELECT status,order_id FROM bot_checkout_attempts WHERE action_key=:key FOR UPDATE'), {'key': key}).mappings().first()
        return {'new': False, **dict(prior)}


def finish_checkout(key, result):
    from sqlalchemy import text
    from database import transaction
    order_id = result.get('order_id') or result.get('duplicate_order_id')
    status = 'succeeded' if order_id else 'uncertain' if result.get('needs_review') else 'failed'
    with transaction() as db:
        db.execute(text('UPDATE bot_checkout_attempts SET status=:status,order_id=:order_id,updated_at=UTC_TIMESTAMP() WHERE action_key=:key'),
                   {'status': status, 'order_id': order_id, 'key': key})


def recover_checkout(key, user_id):
    from database import SessionLocal
    from models.order import Order
    db = SessionLocal()
    try:
        order = db.query(Order.id).filter(Order.checkout_key == key, Order.telegram_id == user_id).first()
        return order.id if order else None
    finally:
        db.close()
