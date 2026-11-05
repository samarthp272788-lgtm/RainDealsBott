"""Warranty policy is captured on purchase; approval never silently changes a wallet."""
from datetime import datetime, timedelta
from html import escape


def summary_text(ctx):
    """Keep replacement details complete within one Telegram message."""
    expires = ctx['expires'].strftime('%d %b %Y %H:%M UTC') if ctx['expires'] else 'Not active'
    text = f'🛡 <b>Order #{ctx["order_id"]} · Warranty</b>\nCoverage: {ctx["days"]} day(s)\nEnds: {expires}\n'
    request = ctx['request']
    if not request:
        return text + ('\n' + escape(ctx['error']) if ctx['error'] else '')
    reason = request['reason']
    text += f'\nStatus: <b>{escape(request["status"])}</b>\nReason: {escape(reason[:250])}'
    if len(reason) > 250:
        text += '…'
    evidence = request['evidence']
    if evidence.get('kind') == 'text':
        value = evidence.get('value', '')
        text += '\nEvidence: ' + escape(value[:250]) + ('…' if len(value) > 250 else '')
    if request['resolution']:
        text += '\n\nResponse / replacement:\n' + escape(request['resolution'])
    return text


def request_text(ctx):
    request = ctx['request']
    text = f'🧾 <b>Order #{ctx["order_id"]} · Request details</b>\n\n' + escape(request['reason'])
    if request['evidence'].get('kind') == 'text':
        text += '\n\n<b>Evidence</b>\n' + escape(request['evidence'].get('value', ''))
    else:
        text += '\n\nPhoto evidence is available from the warranty screen.'
    return text


def start_coverage(order):
    days = int(getattr(order, 'warranty_days', 0) or 0)
    if days and not getattr(order, 'warranty_expires_at', None):
        order.warranty_expires_at = datetime.utcnow() + timedelta(days=days)


def eligibility(order, user_id, now=None):
    if not order or order.telegram_id != user_id:
        return 'Order not found.'
    if order.status != 'completed' or order.refunded:
        return 'Warranty requests are available after delivery for eligible orders.'
    if not getattr(order, 'warranty_days', 0) or not getattr(order, 'warranty_expires_at', None):
        return 'This order has no replacement warranty.'
    if order.warranty_expires_at <= (now or datetime.utcnow()):
        return 'The warranty period has ended.'
    return None


def configure(product_id, days, admin_id):
    from config import ADMIN_IDS
    from database import transaction
    from models.product import Product
    from models.admin_audit_log import AdminAuditLog
    if admin_id not in ADMIN_IDS or not 0 <= days <= 365:
        return 'Choose 0–365 days; 0 disables warranty for new purchases.'
    with transaction() as db:
        product = db.query(Product).filter(Product.id == product_id).with_for_update().first()
        if not product:
            return 'Product not found.'
        product.warranty_days = days
        db.add(AdminAuditLog(admin_telegram_id=admin_id, action='warranty_policy', entity_type='product',
                             entity_id=str(product_id), summary=f'Warranty for new purchases: {days} days.'))
    return None


def context(order_id, user_id, admin=False):
    from config import ADMIN_IDS
    from database import SessionLocal
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    if admin and user_id not in ADMIN_IDS:
        return None
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if not order or (not admin and order.telegram_id != user_id):
            return None
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).first()
        return dict(order_id=order_id, error=eligibility(order, order.telegram_id),
                    reviewable=order.status == 'completed' and not order.refunded,
                    expires=order.warranty_expires_at, days=order.warranty_days,
                    request=None if not request else dict(id=request.id, status=request.status, reason=request.reason,
                         evidence=request.evidence, resolution=request.resolution, admin=request.admin_id))


def submit(order_id, user_id, reason, evidence):
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    from services.input_forms import text_value, InputError
    from services.order_workflow import record_event
    from services.notification_outbox import enqueue
    try:
        reason = text_value(reason, 1500, 10)
        if not isinstance(evidence, dict) or evidence.get('kind') not in ('text', 'photo'):
            return 'Add evidence as a photo or a description.'
        if evidence['kind'] == 'text':
            evidence = {'kind': 'text', 'value': text_value(evidence.get('value'), 2000, 10)}
        else:
            evidence = {'kind': 'photo', 'file_id': text_value(evidence.get('file_id'), 500)}
    except InputError as error:
        return str(error)
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        error = eligibility(order, user_id)
        if error:
            return error
        if db.query(WarrantyRequest.id).filter_by(order_id=order_id).first():
            return 'A warranty request already exists. Open its status from this order.'
        db.add(WarrantyRequest(order_id=order_id, telegram_id=user_id, reason=reason, evidence=evidence, status='submitted'))
        record_event(db, order_id, 'warranty_requested', 'Replacement requested for admin review.', actor_id=user_id)
        for admin_id in set(ADMIN_IDS):
            enqueue(db, f'warranty:{order_id}:submitted', admin_id,
                    f'🛡 <b>Warranty review needed</b>\nOrder #{order_id}\nAdmin → Warranty requests.')
    return None


def review(order_id, admin_id, operation, resolution=None):
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    from services.input_forms import text_value, InputError
    from services.order_workflow import record_event
    from services.notification_outbox import enqueue
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    if operation not in ('approve', 'reject', 'complete'):
        return 'Unknown action.'
    if operation in ('reject', 'complete'):
        try:
            resolution = text_value(resolution, 3000, 1)
        except InputError as error:
            return str(error)
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if not order or not request:
            return 'Request not found.'
        if order.refunded or order.status != 'completed':
            return 'The order is closed or refunded; resolve its status before replacement.'
        expected = 'approved' if operation == 'complete' else 'submitted'
        if request.status != expected:
            return 'This request was already handled. Refresh its status.'
        request.status = {'approve': 'approved', 'reject': 'rejected', 'complete': 'completed'}[operation]
        request.admin_id, request.resolution = admin_id, resolution
        summary = {'approve': 'Replacement approved; awaiting team delivery.',
                   'reject': 'Replacement request declined.', 'complete': 'Replacement delivered.'}[operation]
        record_event(db, order_id, 'warranty_'+request.status, summary, actor_id=admin_id)
        body = f'🛡 <b>Order #{order_id} · Warranty</b>\n{summary}'
        if resolution:
            body += '\n\n' + escape(resolution)
        enqueue(db, f'warranty:{order_id}:{request.status}', order.telegram_id, body)
    return None


def pending(page=0):
    from database import SessionLocal
    from models.bot_operations import WarrantyRequest as Request
    from models.order import Order
    with SessionLocal() as db:
        query = db.query(Request).join(Order, Order.id == Request.order_id).filter(
            Request.status.in_(('submitted', 'approved')), Order.status == 'completed', Order.refunded == False)
        count = query.count()
        rows = query.order_by(Request.created_at.asc()).offset(max(0,page)*8).limit(8).all()
        return count, [dict(order_id=r.order_id, status=r.status) for r in rows]
