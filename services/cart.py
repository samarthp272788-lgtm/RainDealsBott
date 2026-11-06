"""A reviewed cart, processed as individual idempotent orders, never a stock hold."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import secrets

from database import SessionLocal, transaction
from models.customer_tools import CartItem, CartCheckout
from models.product import Product
from models.user import User
from models.order import Order


def _locked_user(db, user_id):
    user = db.query(User).filter_by(telegram_id=user_id).with_for_update().first()
    if not user or user.is_banned:
        raise ValueError('Account unavailable. Send /start or contact support.')
    return user


def _busy(db, user_id):
    return db.query(CartCheckout).filter_by(telegram_id=user_id).filter(CartCheckout.status.in_(['processing', 'held'])).first()


def change(user_id, product_id, quantity):
    if not isinstance(quantity, int) or not 0 <= quantity <= 1000:
        raise ValueError('Quantity must be 0–1,000; zero removes the item.')
    with transaction() as db:
        _locked_user(db, user_id)
        if _busy(db, user_id):
            raise ValueError('A cart checkout is processing or awaiting review. Open its result first.')
        item = db.query(CartItem).filter_by(telegram_id=user_id, product_id=product_id).first()
        if not quantity:
            if item:
                db.delete(item)
            return
        product = db.get(Product, product_id)
        if not product or not product.is_active:
            raise ValueError('Product is unavailable.')
        if Decimal(str(product.price)) == 0:
            quantity = 1
        if item:
            item.quantity, item.revision = quantity, secrets.token_hex(12)
        else:
            if db.query(CartItem.id).filter_by(telegram_id=user_id).count() >= 20:
                raise ValueError('A cart can contain up to 20 different products.')
            db.add(CartItem(telegram_id=user_id, product_id=product_id, quantity=quantity, revision=secrets.token_hex(12)))


def clear(user_id):
    with transaction() as db:
        _locked_user(db,user_id)
        if _busy(db,user_id):
            raise ValueError('Checkout is processing or awaiting review.')
        db.query(CartItem).filter_by(telegram_id=user_id).delete()


def _quote(db, user_id):
    from handlers.products import _active_custom_price, _get_bulk_price, _money
    from services.fulfillment_policy import plan_delivery
    lines = []
    for item in db.query(CartItem).filter_by(telegram_id=user_id).order_by(CartItem.id).all():
        product = db.get(Product, item.product_id)
        price, error = Decimal(0), None
        if not product or not product.is_active:
            error = 'Unavailable'
        else:
            custom = _active_custom_price(db, user_id, product.id)
            price = _money(custom if custom is not None else product.price)
            bulk = _get_bulk_price(product.bulk_pricing, item.quantity) if custom is None and product.bulk_pricing else None
            if bulk is not None:
                price = _money(bulk)
            try:
                if product.source != 'reseller':
                    plan_delivery(product, item.quantity)
                elif product.stock is not None and product.stock < item.quantity:
                    error = 'Insufficient supplier stock'
            except ValueError as exc:
                error = str(exc)
        lines.append(dict(product_id=item.product_id, name=product.name if product else 'Removed product',
                          quantity=item.quantity, revision=item.revision, total=str(_money(price*item.quantity)),
                          source=getattr(product, 'source', 'own'), error=error))
    return lines


def view(user_id):
    with SessionLocal() as db:
        lines = _quote(db,user_id)
        busy = _busy(db,user_id)
        return lines, busy.token if busy else None


def prepare(user_id):
    with transaction() as db:
        user = _locked_user(db,user_id)
        busy = _busy(db,user_id)
        if busy:
            raise ValueError('Your earlier checkout must finish or be reviewed first.')
        lines = _quote(db,user_id)
        if not lines or any(line['error'] for line in lines):
            raise ValueError('Remove unavailable items or adjust quantities before checkout.')
        total = sum((Decimal(line['total']) for line in lines),Decimal(0))
        if Decimal(str(user.balance)) < total:
            raise ValueError(f'Insufficient balance. Cart total: ${total:.8f}.')
        token = secrets.token_hex(12)
        db.add(CartCheckout(token=token,telegram_id=user_id,status='ready',lines=lines,results=[]))
        return token, lines, total


def claim(user_id,token):
    with transaction() as db:
        user = _locked_user(db,user_id)
        batch = db.query(CartCheckout).filter_by(token=token,telegram_id=user_id).with_for_update().first()
        if not batch:
            raise ValueError('Checkout not found.')
        if batch.status != 'ready':
            return None
        if batch.created_at < datetime.utcnow()-timedelta(minutes=5):
            raise ValueError('Price review expired. Review your cart again.')
        busy = _busy(db,user_id)
        if busy and busy.token != token:
            raise ValueError('Another checkout is already processing.')
        fresh = _quote(db,user_id)
        if fresh != batch.lines:
            raise ValueError('Your cart, stock or prices changed. Review the cart again.')
        if Decimal(str(user.balance)) < sum((Decimal(x['total']) for x in fresh),Decimal(0)):
            raise ValueError('Your balance changed. Review the cart again.')
        batch.status = 'processing'
        return list(batch.lines)


def save_result(user_id,token,index,result):
    with transaction() as db:
        batch = db.query(CartCheckout).filter_by(token=token,telegram_id=user_id).with_for_update().one()
        if batch.status != 'processing':
            return False  # A recovery/review took ownership; this worker cannot dispatch another item.
        results = list(batch.results)
        if len(results) != index:
            raise ValueError('Checkout progress changed.')
        line = batch.lines[index]
        order_id = result.get('order_id') or result.get('duplicate_order_id')
        results.append(dict(product_id=line['product_id'], name=line['name'], order_id=order_id,
                            status='ordered' if order_id else 'held' if result.get('needs_review') else 'failed',
                            error='Supplier result requires staff review.' if result.get('needs_review') else str(result.get('error',''))[:250]))
        batch.results = results
        if order_id:
            db.query(CartItem).filter_by(telegram_id=user_id,product_id=line['product_id'],revision=line['revision']).delete()
        if result.get('needs_review'):
            batch.status = 'held'
        elif len(results) == len(batch.lines):
            batch.status = 'completed'
        return batch.status == 'processing'


def result(user_id,token):
    """Recover committed orders after a crash, without replaying supplier requests."""
    with transaction() as db:
        batch = db.query(CartCheckout).filter_by(token=token,telegram_id=user_id).with_for_update().first()
        if not batch:
            raise ValueError('Checkout not found.')
        results = list(batch.results)
        if batch.status in ('held','processing') and batch.updated_at < datetime.utcnow()-timedelta(minutes=5):
            # Only the first unresolved item could have been dispatched.
            index = len(results)
            if index < len(batch.lines):
                line = batch.lines[index]
                key = hashlib.sha256(f'cart:{token}:{index}'.encode()).hexdigest()
                order = db.query(Order).filter_by(checkout_key=key,telegram_id=user_id).first()
                if order:
                    results.append(dict(product_id=line['product_id'],name=line['name'],order_id=order.id,status='ordered',error=''))
                    db.query(CartItem).filter_by(telegram_id=user_id,product_id=line['product_id'],revision=line['revision']).delete()
                    batch.results = results
                    batch.status = 'completed' # Remaining items were never dispatched and remain in cart.
                else:
                    batch.status = 'held'
                    from services.notification_outbox import enqueue_admin
                    enqueue_admin(db,f'cart-held:{token}',f'Interrupted cart {token} for user {user_id} needs reconciliation. Check My Orders and the provider before allowing a retry.',
                                  reply_markup={'inline_keyboard':[[{'text':'Review held cart','callback_data':f'ct_hold_{token}'}]]})
        return dict(token=token,status=batch.status,results=results)


def resolve_hold(admin_id,token,order_id,note):
    from config import ADMIN_IDS
    from models.admin_audit_log import AdminAuditLog
    if admin_id not in ADMIN_IDS:
        raise ValueError('Access denied.')
    if len(note.strip())<20 or len(note)>1000:
        raise ValueError('Describe the provider investigation in 20–1,000 characters.')
    with transaction() as db:
        batch=db.query(CartCheckout).filter_by(token=token).with_for_update().first()
        if not batch or batch.status!='held':
            raise ValueError('Only a held supplier checkout can be reconciled.')
        results=list(batch.results)
        index=next((i for i,r in enumerate(results) if r['status']=='held'),len(results))
        line=batch.lines[index]
        key=hashlib.sha256(f'cart:{token}:{index}'.encode()).hexdigest()
        order=db.query(Order).filter_by(checkout_key=key,telegram_id=batch.telegram_id).first()
        if order_id:
            if not order or order.id!=order_id or order.product_id!=line['product_id'] or order.quantity!=line['quantity']:
                raise ValueError('The order must already be recorded for this exact cart item. Reconcile the supplier result into that order first.')
            row=dict(product_id=line['product_id'],name=line['name'],order_id=order.id,status='ordered',error='')
            db.query(CartItem).filter_by(telegram_id=batch.telegram_id,product_id=line['product_id'],revision=line['revision']).delete()
        else:
            if order:
                raise ValueError('An order exists. Link its order ID instead of declaring no charge.')
            row=dict(product_id=line['product_id'],name=line['name'],order_id=None,status='failed',error='Staff confirmed no supplier order / no charge.')
        if index<len(results): results[index]=row
        else: results.append(row)
        batch.results,batch.status=results,'completed'
        db.add(AdminAuditLog(admin_telegram_id=admin_id,action='resolve_cart_hold',entity_type='cart',entity_id=token,
                              summary=json.dumps({'order_id':order_id,'provider_investigation':note})))


async def checkout(user_id,token,bot_id):
    from handlers.products import _do_purchase, _do_reseller_purchase, _get_purchase_lock
    from services.checkout_actions import claim_checkout, finish_checkout
    from services.notification_outbox import enqueue_admin
    lock = await _get_purchase_lock(user_id)
    async with lock:
        lines = await asyncio.to_thread(claim,user_id,token)
        if lines is None:
            return await asyncio.to_thread(result,user_id,token)
        for index,line in enumerate(lines):
            key = hashlib.sha256(f'cart:{token}:{index}'.encode()).hexdigest()
            claimed = await asyncio.to_thread(claim_checkout,key,user_id,line['product_id'],line['quantity'],True)
            if not claimed['new']:
                outcome = {'duplicate_order_id':claimed['order_id']} if claimed.get('order_id') else {'needs_review':True}
            else:
                try:
                    if line['source']=='reseller':
                        outcome = await _do_reseller_purchase(user_id,line['product_id'],line['quantity'],checkout_key=key,expected_total=line['total'])
                    else:
                        outcome = await asyncio.to_thread(_do_purchase,user_id,line['product_id'],line['quantity'],None,key,line['total'])
                except Exception:
                    outcome = {'needs_review':True}
                await asyncio.to_thread(finish_checkout,key,outcome)
            keep_processing = await asyncio.to_thread(save_result,user_id,token,index,outcome)
            if outcome.get('needs_review'):
                def alert():
                    with transaction() as db:
                        enqueue_admin(db,f'cart-held:{token}',f'Cart checkout {token} needs reconciliation for user {user_id}. Do not reorder the supplier item before checking its result.',
                                      reply_markup={'inline_keyboard':[[{'text':'Review held cart','callback_data':f'ct_hold_{token}'}]]})
                await asyncio.to_thread(alert)
                break
            if not keep_processing:
                break
        return await asyncio.to_thread(result,user_id,token)
