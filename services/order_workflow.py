"""Audit metadata and customer-safe order events. Delivery credentials stay out."""
from datetime import datetime
from html import escape

ACTIVE_STATUSES = ('pending_manual', 'pending_info', 'preorder', 'processing', 'delivery_pending')


def record_event(db, order_id, kind, summary, actor_id=None, customer_visible=True):
    from models.order_workflow import OrderEvent
    db.add(OrderEvent(order_id=order_id, kind=kind, actor_id=actor_id,
                      summary=summary[:300], customer_visible=customer_visible))


def read_timeline(order_id, viewer_id, admin=False):
    from config import ADMIN_IDS
    if admin and viewer_id not in ADMIN_IDS:
        return None
    from database import SessionLocal
    from models.order import Order
    from models.order_workflow import OrderEvent
    from models.order_requirement_submission import OrderRequirementSubmission
    from models.admin_audit_log import AdminAuditLog
    db = SessionLocal()
    try:
        order = db.get(Order, order_id)
        if not order or (not admin and order.telegram_id != viewer_id):
            return None
        query = db.query(OrderEvent).filter(OrderEvent.order_id == order_id)
        if not admin:
            query = query.filter(OrderEvent.customer_visible == True)
        events = [dict(at=r.created_at, sequence=r.id, kind=r.kind, summary=r.summary,
                       actor=r.actor_id if admin else None) for r in query.order_by(OrderEvent.id.desc()).limit(100).all()]
        if not any(e['kind'] == 'created' for e in events):
            events.insert(0, dict(at=order.created_at, kind='created', summary='Order received.', actor=None))
        submission = db.query(OrderRequirementSubmission).filter_by(order_id=order_id).first()
        if submission and not any(e['kind'] == 'information' for e in events):
            events.append(dict(at=submission.submitted_at, kind='information', summary='Customer information received.', actor=None))
        # Old orders may have only the legacy admin audit metadata.
        for audit in db.query(AdminAuditLog).filter(AdminAuditLog.entity_type == 'order', AdminAuditLog.entity_id == str(order_id)).order_by(AdminAuditLog.id.asc()).limit(50).all():
            mapping = {'manual_deliver_order': ('completed', 'Team delivery completed.'), 'refund_order': ('refunded', 'Order refunded.')}
            if audit.action in mapping:
                kind, summary = mapping[audit.action]
                if not any(e['kind'] == kind for e in events):
                    events.append(dict(at=audit.created_at, kind=kind, summary=summary, actor=audit.admin_telegram_id if admin else None))
        events.sort(key=lambda e: (e['at'] or datetime.min, e.get('sequence', 0)))
        return dict(id=order.id, status=order.status, automatic=order.automatic_quantity,
                    remaining=order.manual_pending_quantity, events=events[-25:])
    finally:
        db.close()


def timeline_text(timeline, admin=False):
    lines = [f"🕒 <b>Order #{timeline['id']} · Timeline</b>",
             f"Status: {escape(timeline['status'])}",
             f"Automatic: {timeline['automatic']} · Pending: {timeline['remaining']}", '']
    for event in timeline['events']:
        at = event['at'].strftime('%d %b %H:%M UTC') if event['at'] else 'Time not recorded'
        role = 'User' if event['kind'] in ('created', 'information', 'warranty_requested', 'refund_requested') else 'Admin'
        actor = f" · {role} {event['actor']}" if admin and event.get('actor') else ''
        lines.append(f"• <b>{at}</b>{actor}\n  {escape(event['summary'])}")
    return '\n'.join(lines)


def queue_orders(view, page, admin_id):
    from sqlalchemy import case
    from database import SessionLocal
    from models.order import Order
    with SessionLocal() as db:
        query = db.query(Order).filter(Order.status.in_(ACTIVE_STATUSES), Order.refunded == False)
        if view == 'mine':
            query = query.filter(Order.assigned_admin_id == admin_id)
        elif view == 'unassigned':
            query = query.filter(Order.assigned_admin_id == None)
        elif view == 'ready':
            query = query.filter(Order.status.in_(('pending_manual', 'preorder')))
        elif view == 'info':
            query = query.filter(Order.status == 'pending_info')
        total = query.count()
        page = min(max(0, page), max(0, (total - 1) // 8))
        rows = query.order_by(case((Order.fulfillment_priority == 'high', 0), else_=1),
                              Order.created_at.asc(), Order.id.asc()).offset(page * 8).limit(8).all()
        return [dict(id=o.id, name=o.product_name, status=o.status,
                     automatic=o.automatic_quantity or 0, remaining=remaining_units(o),
                     assigned=o.assigned_admin_id, priority=o.fulfillment_priority or 'normal') for o in rows], page, total


def remaining_units(order):
    if order.status not in ACTIVE_STATUSES or order.refunded:
        return 0
    if order.status == 'delivery_pending':
        return 0
    return order.manual_pending_quantity or max(0, (order.quantity or 1) - (order.automatic_quantity or 0))


def work_details(order_id):
    from database import SessionLocal
    from models.order import Order
    from models.order_workflow import OrderWorkNote
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if not order:
            return None
        notes = db.query(OrderWorkNote).filter_by(order_id=order_id).order_by(OrderWorkNote.id.desc()).limit(3).all()
        return dict(id=order.id, name=order.product_name, status=order.status,
                    remaining=remaining_units(order), automatic=order.automatic_quantity or 0,
                    assigned=order.assigned_admin_id, priority=order.fulfillment_priority or 'normal',
                    active=order.status in ACTIVE_STATUSES and not order.refunded,
                    notes=[dict(admin=n.admin_id, body=n.body, at=n.created_at) for n in notes])


def change_work(order_id, admin_id, operation, value=None):
    from datetime import datetime
    from config import ADMIN_IDS
    from database import transaction
    from models.order import Order
    from models.order_workflow import OrderWorkNote
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    with transaction() as db:
        order = db.query(Order).filter(Order.id == order_id).with_for_update().first()
        if not order:
            return 'Order not found.'
        if operation != 'note' and (order.status not in ACTIVE_STATUSES or order.refunded):
            return 'This order is already closed.'
        if operation == 'claim':
            if order.assigned_admin_id not in (None, admin_id):
                return 'Already assigned. Use Assign admin to reassign it.'
            order.assigned_admin_id, order.assigned_at = admin_id, datetime.utcnow()
            summary = f'Assigned to admin {admin_id}.'
        elif operation == 'assign':
            if value not in ADMIN_IDS:
                return 'Choose a configured admin Telegram ID.'
            order.assigned_admin_id, order.assigned_at = value, datetime.utcnow()
            summary = f'Assigned to admin {value}.'
        elif operation == 'release':
            if order.assigned_admin_id != admin_id:
                return 'Only the assigned admin can release this order.'
            order.assigned_admin_id, order.assigned_at = None, None
            summary = 'Assignment released.'
        elif operation == 'priority':
            order.fulfillment_priority = 'normal' if order.fulfillment_priority == 'high' else 'high'
            summary = f'Priority changed to {order.fulfillment_priority}.'
        elif operation == 'note':
            body = str(value or '').strip()
            if not body or len(body) > 1000:
                return 'Send a note containing 1–1000 characters.'
            db.add(OrderWorkNote(order_id=order_id, admin_id=admin_id, body=body))
            summary = 'Internal note added.'
        else:
            return 'Unknown action.'
        record_event(db, order_id, operation, summary, actor_id=admin_id, customer_visible=False)
        if operation in ('claim', 'assign', 'release'):
            record_event(db, order_id, 'fulfillment_team',
                         'Assigned to the fulfillment team.' if operation != 'release' else 'Returned to the fulfillment queue.')
    return None
