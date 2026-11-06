"""Private, customer-safe notification history with links to owning records."""
from datetime import datetime
from html import unescape
import re

from sqlalchemy import String, cast, func, or_


def _visible(db, user_id, bot_id):
    from models.bot_operations import NotificationJob as Job
    return db.query(Job).filter(
        Job.bot_id == int(bot_id),
        Job.chat_id == str(int(user_id)),
        Job.status.in_(('queued', 'inbox', 'sent', 'uncertain', 'failed')),
        or_(Job.status != 'queued', Job.next_attempt_at <= datetime.utcnow()),
        or_(Job.category.is_(None), Job.category != 'admin'),
        # Older admin cards were accidentally stored under a personal ID and
        # may not have the newer category marker yet. Exclude known admin copy
        # and action keyboards from the customer inbox.
        ~func.lower(Job.body).like('%admin →%'),
        ~Job.body.like('%<b>LOW STOCK ALERT</b>%'),
        ~Job.body.like('%<b>New Support Ticket #%'),
        ~cast(Job.reply_markup, String).like('%warranty_admin_%'),
    )


def _plain(body):
    body = re.sub(r'<[^>]*>', ' ', str(body or ''))
    return ' '.join(unescape(body).split())


def _order_id(row, plain):
    if row.source_order_id:
        return int(row.source_order_id)
    match = re.search(r'(?i)order\s*#\s*(\d{1,12})', plain)
    if match:
        return int(match.group(1))
    return None


def _kind(row, plain):
    category = row.category or ''
    lower = plain.lower()
    if category == 'admin' or 'admin →' in lower:
        return 'admin'
    if category in ('refund', 'warranty') or 'refund' in lower or 'warranty' in lower or 'replacement' in lower:
        if 'refund' in lower:
            return 'refund'
        return 'warranty'
    if category == 'deposit' or 'deposit' in lower or 'payment' in lower or 'invoice' in lower:
        return 'deposit'
    if category == 'support' or 'support' in lower or 'ticket' in lower:
        return 'support'
    if category == 'restock' or 'restock' in lower or 'available again' in lower:
        return 'restock'
    if category == 'subscription' or 'subscription' in lower:
        return 'subscription'
    if category == 'order_progress' or 'order' in lower or 'delivery' in lower or 'delivered' in lower:
        return 'order'
    return 'account'


def _summary(kind, plain, order=None, claim=None):
    lower = plain.lower()
    if kind == 'refund':
        if 'declin' in lower or 'reject' in lower:
            return 'Refund request declined. Open the request to review its status and calculation.'
        if 'approv' in lower or (order and order.refunded):
            amount = getattr(order, 'refund_amount', None) if order else None
            return (f'Refund approved · ${amount:.8f} USDT returned to your bot wallet.'
                    if amount is not None else 'Refund approved. Open the order to view the updated balance and status.')
        if claim and claim.status in ('submitted', 'needs_info'):
            return 'Refund request submitted and awaiting review.'
        return 'Your refund request has an update. Open it to view the current status.'
    if kind == 'warranty':
        if 'declin' in lower or 'reject' in lower:
            return 'Warranty request declined. Open the request to view its current status.'
        if 'approv' in lower:
            return 'Warranty request approved. Open the order to view the next step.'
        if 'deliver' in lower or 'complet' in lower:
            return 'Warranty replacement status updated. Open the order for details.'
        return 'Warranty request updated. Open the order to review its status.'
    if kind == 'deposit':
        if any(word in lower for word in ('credited', 'confirmed', 'completed', 'approved')):
            return 'Payment confirmed. Open Wallet or deposit history to view the credit.'
        if any(word in lower for word in ('failed', 'declined', 'rejected', 'expired')):
            return 'Payment status changed. Open your deposit history for details.'
        return 'Your payment or invoice has an update. Open Wallet to review it.'
    if kind == 'support':
        return 'Support has replied or updated your ticket. Open Support to continue the conversation.'
    if kind == 'restock':
        return 'A product you follow may be available again. Open the shop to check current stock.'
    if kind == 'subscription':
        if 'ended' in lower:
            return 'Your subscription has ended. Open the order to review it.'
        return 'Your subscription is nearing its end. Open the order for the saved expiry date.'
    if kind == 'order':
        if 'information' in lower or 'submit' in lower:
            return 'Information is needed for this order. Open it to submit the requested details.'
        if 'deliver' in lower or 'completed' in lower:
            return 'Your order delivery status changed. Open the order to view delivery details.'
        return 'Your order status changed. Open the order to see the latest details.'
    return 'There is an update for your account. Open the related section to review it.'


def _view_row(row, order=None, claim=None):
    plain = _plain(row.body)
    kind = _kind(row, plain)
    oid = _order_id(row, plain)
    order_text = None
    if order:
        product_name = (order.product_name or 'Product').strip()
        order_text = f"Order #{order.id} — {product_name[:65]}"
    elif oid:
        order_text = f'Order #{oid}'
    else:
        deposit_match = re.search(r'(?i)deposit\s*#\s*(\d{1,12})', plain)
        ticket_match = re.search(r'(?i)ticket\s*#\s*(\d{1,12})', plain)
        product_match = re.search(r'(?i)back in stock\s+(.+?)\s+price\s*:', plain)
        order_text = (f'Deposit #{deposit_match.group(1)} · Payment update' if deposit_match else
                      f'Ticket #{ticket_match.group(1)} · Support update' if ticket_match else
                      f"{product_match.group(1)[:65]} · Restock" if product_match else
                      'Wallet · Payment update' if kind == 'deposit' else
                      'Support · Ticket update' if kind == 'support' else
                      'Shop · Restock update' if kind == 'restock' else
                      'Subscription reminder' if kind == 'subscription' else 'Account update')
    if kind == 'refund':
        title = f'{order_text} · Refund update'
    elif kind == 'warranty':
        title = f'{order_text} · Warranty update'
    elif kind == 'order':
        title = f'{order_text} · Order update'
    else:
        title = order_text
    return dict(
        id=row.id,
        title=title[:120],
        summary=_summary(kind, plain, order, claim),
        kind=kind,
        order_id=order.id if order else oid,
        created_at=row.created_at,
        unread=row.inbox_read_at is None,
        # Never replay stored keyboards here: legacy admin cards could contain
        # privileged callbacks. The detail handler builds customer-safe links.
        reply_markup=None,
        order=(dict(id=order.id, product_name=order.product_name, status=order.status,
                    quantity=order.quantity, amount=order.amount, created_at=order.created_at,
                    refunded=bool(order.refunded), refund_amount=order.refund_amount) if order else None),
        claim=(dict(request_type=claim.request_type, status=claim.status,
                    refund_amount=claim.refund_amount, used_days=claim.used_days,
                    remaining_days=claim.remaining_days, created_at=claim.created_at) if claim else None),
    )


def unread_count(db, user_id, bot_id=None):
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    return _visible(db, user_id, bot_id or main_bot_id()).filter(Job.inbox_read_at.is_(None)).count()


def count(user_id, bot_id=None):
    from database import SessionLocal
    with SessionLocal() as db:
        return unread_count(db, user_id, bot_id)


def _related_records(db, user_id, rows):
    from models.order import Order
    from models.bot_operations import WarrantyRequest
    ids = {_order_id(row, _plain(row.body)) for row in rows}
    ids.discard(None)
    if not ids:
        return {}, {}
    orders = db.query(Order).filter(Order.telegram_id == int(user_id), Order.id.in_(ids)).all()
    by_id = {order.id: order for order in orders}
    claims = db.query(WarrantyRequest).filter(WarrantyRequest.telegram_id == int(user_id),
                                               WarrantyRequest.order_id.in_(by_id)).all()
    return by_id, {claim.order_id: claim for claim in claims}


def listing(user_id, page=0, bot_id=None):
    from database import SessionLocal
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    page = max(0, min(int(page), 100000))
    with SessionLocal() as db:
        query = _visible(db, user_id, bot_id or main_bot_id())
        rows = query.order_by(Job.id.desc()).offset(page * 7).limit(8).all()
        orders, claims = _related_records(db, user_id, rows)
        newest = query.order_by(Job.id.desc()).first()
        return dict(
            unread=query.filter(Job.inbox_read_at.is_(None)).count(),
            page=page,
            has_more=len(rows) > 7,
            newest_id=newest.id if newest else 0,
            rows=[_view_row(row, orders.get(_order_id(row, _plain(row.body))),
                            claims.get(_order_id(row, _plain(row.body)))) for row in rows[:7]],
        )


def read(user_id, job_id, bot_id=None):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    with transaction() as db:
        row = _visible(db, user_id, bot_id or main_bot_id()).filter(Job.id == int(job_id)).with_for_update().first()
        if not row:
            raise ValueError('Notification not found.')
        if row.inbox_read_at is None:
            row.inbox_read_at = datetime.utcnow()
        orders, claims = _related_records(db, user_id, [row])
        order_id = _order_id(row, _plain(row.body))
        return _view_row(row, orders.get(order_id), claims.get(order_id))


def mark_read(user_id, through_id, bot_id=None):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    through_id = int(through_id)
    if not 0 <= through_id <= 2**63 - 1:
        raise ValueError('Invalid notification page.')
    with transaction() as db:
        return _visible(db, user_id, bot_id or main_bot_id()).filter(
            Job.id <= through_id, Job.inbox_read_at.is_(None)
        ).update({Job.inbox_read_at: datetime.utcnow()}, synchronize_session=False)


def badge(unread):
    count = max(0, int(unread))
    label = '99+' if count > 99 else str(count)
    return '🔔 Notifications' + (f' · {label} new' if count else '')


async def refresh_home_badge(bot, user_id):
    """Update only a still-visible Home keyboard when its inbox receives an item."""
    import asyncio
    from aiogram.types import InlineKeyboardMarkup
    from services.screen_controller import screens, locations
    key = (int(user_id), int(user_id))
    entry = screens.get(key)
    if not entry or not entry.prompt:
        return
    version = entry.version
    markup = entry.prompt.get('reply_markup') or {}
    if hasattr(markup, 'model_dump'):
        markup = markup.model_dump(mode='json', exclude_none=True)
    callbacks = {b.get('callback_data') for row in markup.get('inline_keyboard', []) for b in row}
    if not {'ct_inbox', 'products_menu', 'deposit_start'} <= callbacks:
        return
    unread = await asyncio.to_thread(count, user_id, bot.id)
    async with entry.lock:
        if entry.version != version or screens.get(key) is not entry:
            return
        location = locations.get(key)
        if not location:
            return
        markup = InlineKeyboardMarkup.model_validate(markup).model_dump(mode='json', exclude_none=True)
        changed = False
        for row in markup['inline_keyboard']:
            for button in row:
                if button.get('callback_data') == 'ct_inbox' and button['text'] != badge(unread):
                    button['text'] = badge(unread)
                    changed = True
        if changed:
            try:
                await bot.edit_message_reply_markup(chat_id=location[0], message_id=location[1],
                                                    reply_markup=InlineKeyboardMarkup.model_validate(markup))
            except Exception:
                return
            entry.prompt = {**entry.prompt, 'reply_markup': markup}
