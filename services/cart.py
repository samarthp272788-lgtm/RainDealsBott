"""A reviewed cart, processed as individual idempotent orders, never a stock hold."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import logging
import secrets

from database import SessionLocal, transaction
from models.customer_tools import CartItem, CartCheckout
from models.product import Product
from models.user import User
from models.order import Order

logger = logging.getLogger(__name__)


def _reseller_stock(product):
    """Use the same cached live-stock value shown by the shop."""
    from handlers.products import _real_stock
    return max(0, int(_real_stock(product)))


def _locked_user(db, user_id):
    user = db.query(User).filter_by(telegram_id=user_id).with_for_update().first()
    if not user or user.is_banned:
        raise ValueError('Account unavailable. Send /start or contact support.')
    return user


def _busy(db, user_id):
    return db.query(CartCheckout).filter_by(telegram_id=user_id).filter(CartCheckout.status.in_(['processing', 'held'])).first()


def _checkout_status(results, line_count):
    """Distinguish a finished batch from a fully successful batch."""
    if len(results) < line_count:
        return 'partial' if any(row.get('order_id') for row in results) else 'failed'
    successful = sum(bool(row.get('order_id')) for row in results)
    if successful == line_count:
        return 'completed'
    return 'partial' if successful else 'failed'


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
        from handlers.products import _product_purchase_configuration_errors
        config_errors = _product_purchase_configuration_errors(product)
        if config_errors:
            raise ValueError('Product checkout is temporarily disabled because its purchase settings need repair.')
        if Decimal(str(product.price)) < 0:
            raise ValueError('Product is unavailable because its price is invalid.')
        is_free = Decimal(str(product.price)) == 0
        if is_free:
            quantity = 1
        if product.source == 'reseller':
            available = _reseller_stock(product)
            if quantity > available:
                raise ValueError(f'Only {available} supplier units are currently available.')
        else:
            from services.fulfillment_policy import plan_delivery
            try:
                plan_delivery(product, quantity)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
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
    from handlers.products import _active_custom_price, _get_bulk_price, _money, _product_purchase_configuration_errors
    from services.fulfillment_policy import plan_delivery, effective_delivery_type
    lines = []
    for item in db.query(CartItem).filter_by(telegram_id=user_id).order_by(CartItem.id).all():
        product = db.get(Product, item.product_id)
        price, error, plan = Decimal(0), None, None
        if not product or not product.is_active:
            error = 'Unavailable'
        else:
            config_errors = _product_purchase_configuration_errors(product)
            if config_errors:
                error = 'Product checkout is temporarily disabled because its purchase settings need repair.'
            elif Decimal(str(product.price)) < 0:
                error = 'Product is unavailable because its price is invalid.'
            else:
                custom = _active_custom_price(db, user_id, product.id)
                price = _money(custom if custom is not None else product.price)
                bulk = _get_bulk_price(product.bulk_pricing, item.quantity) if custom is None and product.bulk_pricing else None
                if bulk is not None:
                    price = _money(bulk)
                try:
                    if product.source != 'reseller':
                        plan = plan_delivery(product, item.quantity)
                    else:
                        available = _reseller_stock(product)
                        if item.quantity > available:
                            error = f'Only {available} supplier units are currently available'
                except ValueError as exc:
                    error = str(exc)
        requirements = getattr(product, 'required_info', None) if product else None
        settings = {
            'delivery_type': getattr(product, 'delivery_type', None),
            'effective_delivery_type': effective_delivery_type(product) if product else None,
            'manual_fallback': bool(getattr(product, 'manual_fallback', False)),
            'preorder': bool(getattr(product, 'preorder', False)),
            'required_info': requirements,
            'delivery_instruction': getattr(product, 'delivery_instruction', None),
            'aftercare_policy': getattr(product, 'aftercare_policy', None),
            'warranty_days': getattr(product, 'warranty_days', 0),
            'subscription_days': getattr(product, 'subscription_days', 0),
            'term_days': getattr(product, 'term_days', 0),
        }
        settings_fingerprint = hashlib.sha256(json.dumps(
            settings, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str
        ).encode('utf-8')).hexdigest()
        lines.append(dict(product_id=item.product_id, name=product.name if product else 'Removed product',
                          quantity=item.quantity, revision=item.revision, total=str(_money(price*item.quantity)),
                          source=getattr(product, 'source', 'own'),
                          delivery_mode=(plan.order_mode if plan else settings['effective_delivery_type']),
                          automatic_quantity=(plan.automatic_quantity if plan else item.quantity if product and product.source == 'reseller' else 0),
                          manual_quantity=(plan.manual_quantity if plan else 0),
                          settings_fingerprint=settings_fingerprint, error=error))
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
        # A cart has one live review card. Old Telegram buttons remain harmless
        # because claim() accepts only a row whose status is still "ready".
        db.query(CartCheckout).filter_by(telegram_id=user_id, status='ready').update(
            {CartCheckout.status: 'expired'}, synchronize_session=False
        )
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
            batch.status = _checkout_status(results, len(batch.lines))
        return batch.status == 'processing'


def touch_processing(user_id, token):
    """Heartbeat a live checkout so a slow supplier call is not mistaken for a crash."""
    with transaction() as db:
        batch = db.query(CartCheckout).filter_by(
            token=token, telegram_id=user_id, status='processing'
        ).with_for_update().first()
        if not batch:
            return False
        batch.updated_at = datetime.utcnow()
        return True


def result(user_id,token):
    """Recover committed orders after a crash, without replaying supplier requests."""
    with transaction() as db:
        batch = db.query(CartCheckout).filter_by(token=token,telegram_id=user_id).with_for_update().first()
        if not batch:
            raise ValueError('Checkout not found.')
        results = list(batch.results)
        if batch.status in ('held','processing') and batch.updated_at < datetime.utcnow()-timedelta(minutes=5):
            # A result already marked held is the unresolved item. Otherwise
            # the first item without a saved result may have been dispatched.
            index = next((i for i, row in enumerate(results) if row.get('status') == 'held'), len(results))
            if index < len(batch.lines):
                line = batch.lines[index]
                key = hashlib.sha256(f'cart:{token}:{index}'.encode()).hexdigest()
                order = db.query(Order).filter_by(checkout_key=key,telegram_id=user_id).first()
                if order:
                    recovered = dict(product_id=line['product_id'],name=line['name'],order_id=order.id,status='ordered',error='')
                    if index < len(results):
                        results[index] = recovered
                    else:
                        results.append(recovered)
                    db.query(CartItem).filter_by(telegram_id=user_id,product_id=line['product_id'],revision=line['revision']).delete()
                    batch.results = results
                    batch.status = _checkout_status(results, len(batch.lines))
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
        batch.results,batch.status=results,_checkout_status(results,len(batch.lines))
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
        async def heartbeat():
            while True:
                await asyncio.sleep(45)
                try:
                    active = await asyncio.to_thread(touch_processing,user_id,token)
                except Exception:
                    logger.warning("Cart checkout heartbeat failed temporarily")
                    continue
                if not active:
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
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
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
