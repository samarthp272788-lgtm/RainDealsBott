"""Authoritative claim invoices, evidence and recovery of recorded delivery times."""
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

ISSUES = {'deactivated':'Account deactivated', 'login':'Cannot log in',
          'subscription':'Subscription missing', 'other':'Other issue'}
MAX_PROOF_BYTES = 2 * 1024 * 1024


def proof_mime(data):
    if not isinstance(data, bytes) or not 3 <= len(data) <= MAX_PROOF_BYTES:
        raise ValueError('Upload a screenshot smaller than 2 MB.')
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('Proof must be a JPG, PNG or WebP screenshot.')


def _optional_first(db, sql, params):
    try:
        return db.execute(text(sql), params).first()
    except (OperationalError, ProgrammingError) as error:
        if not any(s in str(error.orig).lower() for s in ("doesn't exist", 'no such table', 'unknown column', 'no such column')):
            raise
        return None


def recover_delivery_date(db, order):
    """Never substitute today's time for a previously fulfilled manual order."""
    if order.status != 'completed' or getattr(order,'coverage_started_at',None):
        return False
    date = None
    days = getattr(order,'warranty_days',0) or 0
    if days and order.warranty_expires_at:
        date = order.warranty_expires_at - timedelta(days=days)
    if not date:
        for sql in (
            "SELECT MIN(created_at) FROM order_events WHERE order_id=:id AND kind='completed'",
            "SELECT MIN(created_at) FROM raindeals_web_manager_audit WHERE target_type='order' AND target_id=:key AND action='fulfill'",
            "SELECT MIN(created_at) FROM admin_audit_logs WHERE entity_type='order' AND entity_id=:key AND action='manual_deliver_order'",
            "SELECT MIN(completed_at) FROM raindeals_delivery_bot_jobs WHERE order_id=:id AND status='sent'",
        ):
            row = _optional_first(db,sql,{'id':order.id,'key':str(order.id)})
            if row and row[0]:
                date = row[0]
                break
    if isinstance(date,str):
        date = datetime.fromisoformat(date.replace('Z','+00:00')).replace(tzinfo=None)
    if not date and order.delivery_type == 'automatic' and not order.reseller_id and order.delivered_account:
        date = order.created_at  # Native automatic delivery completes in the purchase transaction.
    if not date or date < order.created_at or date > datetime.utcnow():
        return False
    order.coverage_started_at = date
    if days and not order.warranty_expires_at:
        order.warranty_expires_at = date + timedelta(days=days)
    return True


def missing_metadata(order):
    from services.warranty import policy
    mode = policy(order)
    if order.status != 'completed' or order.refunded:
        return False
    return (mode == 'refund' and (not order.subscription_days or not order.coverage_started_at)) or (
        mode == 'warranty' and bool(order.warranty_days) and not order.warranty_expires_at)


def invoice(order):
    from services.warranty import policy
    return dict(reference=f'RD-{order.id}', order_id=order.id, product_id=order.product_id,
                product=order.product_name, quantity=order.quantity, paid=str(order.amount),
                unit_paid=str(Decimal(str(order.amount))/max(1,order.quantity or 1)),
                customer_id=str(order.telegram_id), purchased_at=order.created_at.isoformat() if order.created_at else None,
                delivered_at=order.coverage_started_at.isoformat() if order.coverage_started_at else None,
                policy=policy(order), warranty_days=order.warranty_days,
                subscription_days=order.subscription_days, delivery_type=order.delivery_type,
                supplier_reference=order.reseller_order_id)


def invoice_text(value):
    return ('🧾 <b>Order invoice '+escape(value['reference'])+'</b>\n'
            f"Product: <b>{escape(value['product'] or 'Product')}</b>\n"
            f"Product ID: #{value['product_id']} · Quantity: {value['quantity']}\n"
            f"Paid: <b>${escape(value['paid'])} USDT</b>\n"
            f"Buyer: <code>{escape(value['customer_id'])}</code>\n"
            f"Purchased (UTC): {escape(value['purchased_at'] or 'Not recorded')}\n"
            f"Delivered (UTC): {escape(value['delivered_at'] or 'Needs staff confirmation')}")


def metadata_invoice(saved,order):
    value={**invoice(order),**(saved or {})}
    value.update(delivered_at=order.coverage_started_at.isoformat() if order.coverage_started_at else value.get('delivered_at'),
                 warranty_days=order.warranty_days,subscription_days=order.subscription_days)
    return value
