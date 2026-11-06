"""Customer actions with ownership checks and durable notification receipts."""
from datetime import datetime, timedelta
from decimal import Decimal
import secrets

from database import SessionLocal, transaction
from models.order import Order
from models.user import User
from models.bot_operations import WarrantyRequest
from models.customer_tools import CustomerPreferences, ClaimMessage, CancellationRequest, WalletEntry
from services.notification_outbox import enqueue, enqueue_admin
from services.order_workflow import record_event


def preferences(user_id, toggle=None):
    with transaction() as db:
        user = db.query(User).filter_by(telegram_id=user_id).with_for_update().first()
        if not user:
            raise ValueError('Send /start first.')
        row = db.get(CustomerPreferences, user_id)
        if not row:
            row = CustomerPreferences(telegram_id=user_id, restock=True, order_progress=True, subscription=True)
            db.add(row)
        if toggle:
            if toggle not in ('restock', 'order_progress', 'subscription'):
                raise ValueError('Unknown preference.')
            setattr(row, toggle, not getattr(row, toggle))
        return {key: bool(getattr(row, key)) for key in ('restock', 'order_progress', 'subscription')}


def allows(db, user_id, category):
    if category not in ('restock', 'order_progress', 'subscription'):
        return True  # Financial receipts and security notices cannot be disabled.
    row = db.get(CustomerPreferences, user_id)
    return row is None or bool(getattr(row, category))


def receive_order(user_id, order_id):
    with transaction() as db:
        order = db.query(Order).filter_by(id=order_id, telegram_id=user_id).with_for_update().first()
        if not order or order.status != 'completed' or order.refunded:
            raise ValueError('Delivery is not complete for this order.')
        if not order.received_at:
            order.received_at = datetime.utcnow()
            record_event(db, order_id, 'received', 'Customer confirmed receipt.', actor_id=user_id)
        return order.received_at


def cancellable(order):
    return bool(order and not order.refunded and order.status in ('pending_manual', 'pending_info', 'preorder')
                and not order.delivered_account and not order.reseller_order_id and not order.reseller_id)


def request_cancellation(user_id, order_id, reason):
    reason = reason.strip()
    if not 5 <= len(reason) <= 1000:
        raise ValueError('Write a reason of 5–1,000 characters.')
    with transaction() as db:
        order = db.query(Order).filter_by(id=order_id, telegram_id=user_id).with_for_update().first()
        if not cancellable(order):
            raise ValueError('Cancellation is available before delivery starts. Contact support for this order.')
        existing = db.get(CancellationRequest, order_id)
        if existing:
            return existing.status
        from services.notification_outbox import main_bot_id
        db.add(CancellationRequest(order_id=order_id, telegram_id=user_id, reason=reason, bot_id=main_bot_id()))
        record_event(db, order_id, 'cancel_requested', reason, actor_id=user_id)
        enqueue_admin(db, f'cancel-request:{order_id}', f'Cancellation requested for order #{order_id}. Review in Admin → Orders.',
                      reply_markup={'inline_keyboard': [[{'text': 'Review cancellation', 'callback_data': f'ct_cancelreview_{order_id}'}]]})
        return 'submitted'


def cancellation_context(user_id, order_id, admin=False):
    from config import ADMIN_IDS
    if admin and user_id not in ADMIN_IDS:
        raise ValueError('Access denied.')
    with SessionLocal() as db:
        row = db.get(CancellationRequest, order_id)
        if not row or (not admin and row.telegram_id != user_id):
            return None
        return dict(status=row.status, reason=row.reason, resolution=row.resolution)


def review_cancellation(admin_id, order_id, accept):
    from config import ADMIN_IDS
    if admin_id not in ADMIN_IDS:
        raise ValueError('Access denied.')
    # _do_refund locks the order and checks cancellability again in that same transaction.
    if accept:
        from handlers.admin_orders import _do_refund
        result = _do_refund(order_id, admin_id, cancellation_only=True)
        if result.get('error'):
            raise ValueError(result['error'])
        return 'approved'
    with transaction() as db:
        order = db.query(Order).filter_by(id=order_id).with_for_update().first()
        row = db.get(CancellationRequest, order_id)
        if not order or not row or row.status != 'submitted':
            raise ValueError('Request already resolved or unavailable.')
        row.status, row.admin_id = 'declined', admin_id
        row.resolution = 'Cancellation declined. Open support if you need help.'
        record_event(db, order_id, 'cancel_declined', row.resolution, actor_id=admin_id)
        enqueue(db, f'cancel-result:{order_id}', order.telegram_id, f'Order #{order_id}: {row.resolution}')
        return row.status


def claim_conversation(user_id, order_id, admin=False):
    from config import ADMIN_IDS
    if admin and user_id not in ADMIN_IDS:
        raise ValueError('Access denied.')
    with SessionLocal() as db:
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).first()
        if not request or (not admin and request.telegram_id != user_id):
            raise ValueError('Claim not found.')
        rows = db.query(ClaimMessage).filter_by(order_id=order_id).order_by(ClaimMessage.id.desc()).limit(20).all()
        return request.status, [dict(id=r.id, role=r.role, text=r.text, date=r.created_at, photo=bool(r.mime)) for r in reversed(rows)]


def claim_reply(user_id, order_id, text, admin=False, image=None):
    from config import ADMIN_IDS
    from services.claim_details import proof_mime
    if admin and user_id not in ADMIN_IDS:
        raise ValueError('Access denied.')
    if not 5 <= len(text.strip()) <= 1500:
        raise ValueError('Write 5–1,500 characters explaining what is needed or providing your answer.')
    mime = proof_mime(image) if image is not None else None
    with transaction() as db:
        order = db.query(Order).filter_by(id=order_id).with_for_update().first()
        request = db.query(WarrantyRequest).filter_by(order_id=order_id).with_for_update().first()
        if not request or not order or order.status != 'completed' or order.refunded or (not admin and request.telegram_id != user_id):
            raise ValueError('Claim not found.')
        if request.status not in (('submitted', 'needs_info') if admin else ('needs_info',)):
            raise ValueError('This claim is not waiting for a reply.')
        row = ClaimMessage(order_id=order_id, actor_id=user_id, role='admin' if admin else 'customer',
                           text=text.strip(), image=image, mime=mime)
        db.add(row)
        db.flush()
        request.status = 'needs_info' if admin else 'submitted'
        record_event(db, order_id, 'claim_information_requested' if admin else 'claim_information_received',
                     'Claim follow-up recorded.', actor_id=user_id)
        # The original invoice, claim date and refund quote are intentionally unchanged.
        if admin:
            enqueue(db, f'claim-reply:{row.id}', request.telegram_id,
                    f'Order #{order_id}: support needs more information. Open My Orders → Claim → Conversation.',
                    reply_markup={'inline_keyboard': [[{'text': 'Reply to claim', 'callback_data': f'ct_thread_{order_id}'}]]})
        else:
            enqueue_admin(db, f'claim-reply:{row.id}', f'New customer evidence for order #{order_id}.',
                          reply_markup={'inline_keyboard': [[{'text': 'Review claim', 'callback_data': f'warranty_admin_{order_id}'}]]})


def claim_reply_image(user_id, message_id, admin=False):
    from config import ADMIN_IDS
    with SessionLocal() as db:
        row = db.get(ClaimMessage, message_id)
        claim = db.query(WarrantyRequest).filter_by(order_id=row.order_id).first() if row else None
        if not claim or (user_id != claim.telegram_id and not (admin and user_id in ADMIN_IDS)):
            raise ValueError('Evidence not found.')
        return row.image, row.order_id


def wallet_statement(user_id, page=0):
    """Reconcile the shared wallet explicitly; never invent a dated credit history."""
    with transaction() as db:
        user = db.query(User).filter_by(telegram_id=user_id).with_for_update().first()
        if not user:
            raise ValueError('User not found.')
        latest = db.query(WalletEntry).filter_by(telegram_id=user_id).order_by(WalletEntry.id.desc()).with_for_update().first()
        balance = Decimal(str(user.balance or 0))
        prior = Decimal(str(latest.balance)) if latest else Decimal(0)
        if latest is None or balance != prior:
            db.add(WalletEntry(telegram_id=user_id, event_key=secrets.token_hex(24),
                               label='Opening balance' if latest is None else 'External wallet adjustment / reconciliation',
                               amount=balance-prior, balance=balance))
            db.flush()
        rows = db.query(WalletEntry).filter_by(telegram_id=user_id).order_by(WalletEntry.id.desc()).offset(page*8).limit(9).all()
        return balance, [dict(label=r.label, amount=r.amount, balance=r.balance, date=r.created_at) for r in rows[:8]], len(rows)>8


from services.wallet_ledger import record_wallet


def subscription_reminders():
    from models.customer_tools import CustomerJobCursor
    now = datetime.utcnow()
    with transaction() as db:
        cursor = db.query(CustomerJobCursor).filter_by(name='subscriptions').with_for_update().first()
        if not cursor:
            cursor = CustomerJobCursor(name='subscriptions',last_order_id=0)
            db.add(cursor)
        orders = db.query(Order).filter(Order.status=='completed', Order.refunded==False,
                    Order.id > cursor.last_order_id,
                    Order.subscription_expires_at > now-timedelta(days=1),
                    Order.subscription_expires_at <= now+timedelta(days=3)).order_by(Order.id).limit(500).all()
        cursor.last_order_id = orders[-1].id if len(orders)==500 else 0
        for order in orders:
            if not allows(db, order.telegram_id, 'subscription'):
                continue
            seconds = (order.subscription_expires_at-now).total_seconds()
            stage = 'ended' if seconds<=0 else '1day' if seconds<=86400 else '3days'
            enqueue(db, f'subscription:{order.id}:{order.subscription_expires_at.isoformat()}:{stage}', order.telegram_id,
                    f'Order #{order.id}: subscription '+('has ended.' if seconds<=0 else f'ends on {order.subscription_expires_at:%d %b %Y %H:%M} UTC.')+
                    '\nOpen the order to view details. Renewal requires a new purchase; no automatic charge.',
                    reply_markup={'inline_keyboard': [[{'text': 'View order', 'callback_data': f'order_detail_{order.id}'}]]})
