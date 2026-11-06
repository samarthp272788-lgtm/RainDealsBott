"""Durable sends. Ambiguous Telegram timeouts are held for review, never replayed."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import logging
from sqlalchemy import func
from sqlalchemy.orm import aliased

logger = logging.getLogger(__name__)


def main_bot_id():
    from config import BOT_TOKEN
    return int(BOT_TOKEN.split(':', 1)[0])


def admin_destination():
    import config
    return str(getattr(config, 'ADMIN_ORDER_NOTIFICATION_CHANNEL_ID', '') or
               getattr(config, 'NOTIFICATION_CHANNEL_ID', '')).strip()


def enqueue_admin(db, event, body, **kwargs):
    """Operational alerts go to Admin Notify, never private shop conversations."""
    destination = admin_destination()
    if not destination:
        logger.warning('Admin notification destination is not configured')
        return
    enqueue(db, f'admin:{event}', destination, body, **kwargs)


def legacy_admin_alert(row):
    markup = str(row.reply_markup or '')
    return ('warranty_admin_' in markup or '<b>LOW STOCK ALERT</b>' in row.body
            or '<b>New Support Ticket #' in row.body)


def _normalise_destination(bot_id, chat_id):
    """Keep main-bot admin alerts out of private admin chats."""
    unroutable = False
    if int(bot_id) == main_bot_id() and str(chat_id).isdigit():
        from config import ADMIN_IDS
        if int(chat_id) in ADMIN_IDS:
            destination = admin_destination()
            if destination:
                return destination, False
            unroutable = True
    return str(chat_id), unroutable


def private_admin_alert_cards(user_id, bot_id):
    """Known old alerts can be cleaned without touching customer receipts."""
    from config import ADMIN_IDS
    from database import SessionLocal
    from models.bot_operations import NotificationJob
    if user_id not in ADMIN_IDS:
        return []
    with SessionLocal() as db:
        rows = db.query(NotificationJob).filter_by(bot_id=bot_id, chat_id=str(user_id), status='sent').filter(
            NotificationJob.message_id.is_not(None)).order_by(NotificationJob.id.desc()).limit(100).all()
        return [(user_id, row.message_id) for row in rows if legacy_admin_alert(row)]


def reroute_pending_admin_alerts(bot_id):
    from config import ADMIN_IDS
    from database import transaction
    from models.bot_operations import NotificationJob
    destination = admin_destination()
    with transaction() as db:
        rows = db.query(NotificationJob).filter(NotificationJob.bot_id == bot_id,
                    NotificationJob.chat_id.in_([str(i) for i in ADMIN_IDS]),
                    NotificationJob.status.in_(['queued', 'failed'])).order_by(NotificationJob.id).limit(500).with_for_update().all()
        for row in rows:
            if not destination:
                row.status, row.error_code, row.category = 'failed', 'admin_notify_unconfigured', 'admin'
                continue
            digest = hashlib.sha256((row.body+str(row.reply_markup)).encode()).hexdigest()
            enqueue(db, f'admin:reroute-admin:{digest}', destination, row.body, bot_id, reply_markup=row.reply_markup)
            db.flush()  # The next old per-admin copy must see this unique notification.
            row.status, row.category = 'resolved', 'admin'
            row.error_code = 'moved_to_admin_notify'


def enqueue(db, event, chat_id, body, bot_id=None, source_order_id=None, reply_markup=None):
    from models.bot_operations import NotificationJob
    bot_id = bot_id or main_bot_id()
    admin_notice = str(event).lower().startswith('admin:')
    if not admin_notice and int(bot_id) == main_bot_id() and str(chat_id).isdigit():
        from config import ADMIN_IDS
        admin_notice = int(chat_id) in ADMIN_IDS
    chat_id, unroutable_admin_notice = _normalise_destination(bot_id, chat_id)
    prefix = str(event).lower().split(':', 1)[0]
    category = ('admin' if admin_notice else
                'subscription' if prefix == 'subscription' else
                'restock' if prefix == 'restock' else
                'order_progress' if prefix in ('delivery-start', 'manual-delivery', 'order-information', 'order-progress', 'cancel-result') else
                'refund' if prefix in ('refund', 'manager-refund') or (prefix == 'warranty' and 'refund' in str(event).lower()) else
                'warranty' if prefix == 'warranty' else
                'deposit' if prefix.startswith('deposit') else
                'support' if prefix in ('ticket-status', 'manager-ticket', 'claim-reply') else None)
    if category in ('subscription','restock','order_progress') and str(chat_id).isdigit():
        from services.customer_tools import allows
        if not allows(db, int(chat_id), category):
            return
    if source_order_id is None and category in ('subscription','order_progress','refund','warranty'):
        import re
        parts = str(event).split(':')
        order_event = parts[0].lower() in ('subscription','delivery-start','manual-delivery','order-information',
                                           'order-progress','cancel-result','refund','warranty','manager-refund')
        if order_event and len(parts)>1 and parts[1].isdigit():
            source_order_id = int(parts[1])
        else:
            match = re.search(r'(?i)order\s*#\s*(\d{1,12})', str(body))
            if match:
                source_order_id = int(match.group(1))
    key = hashlib.sha256(f'{bot_id}:{event}:{chat_id}'.encode()).hexdigest()
    # Callers hold their originating order/request lock. Unique key is an extra backstop.
    if db.query(NotificationJob.id).filter_by(event_key=key).first():
        return
    if unroutable_admin_notice:
        logger.warning('Admin notification retained in failed outbox: Admin Notify destination is not configured')
    if reply_markup is not None:
        from aiogram.types import InlineKeyboardMarkup
        reply_markup = InlineKeyboardMarkup.model_validate(reply_markup).model_dump(mode='json', exclude_none=True)
    db.add(NotificationJob(event_key=key, bot_id=bot_id, chat_id=str(chat_id), body=body,
                           status='failed' if unroutable_admin_notice else 'queued', attempts=0, next_attempt_at=datetime.utcnow(),
                           error_code='admin_notify_unconfigured' if unroutable_admin_notice else None,
                           source_order_id=source_order_id,
                           reply_markup=reply_markup, category=category))


def enqueue_once(event, chat_id, body, bot_id=None, reply_markup=None):
    from database import transaction, SessionLocal
    from sqlalchemy.exc import IntegrityError
    from models.bot_operations import NotificationJob
    bot_id = bot_id or main_bot_id()
    normalized_chat_id, _ = _normalise_destination(bot_id, chat_id)
    try:
        with transaction() as db:
            enqueue(db, event, chat_id, body, bot_id, reply_markup=reply_markup)
    except IntegrityError:
        key = hashlib.sha256(f'{bot_id}:{event}:{normalized_chat_id}'.encode()).hexdigest()
        with SessionLocal() as db:
            if not db.query(NotificationJob.id).filter_by(event_key=key).first():
                raise


async def queue_message(bot, event, chat_id, text, reply_markup=None, parse_mode='HTML'):
    if parse_mode not in ('HTML', None):
        raise ValueError('The notification outbox accepts HTML messages only.')
    if parse_mode is None:
        from html import escape
        text = escape(str(text))
    await asyncio.to_thread(enqueue_once, event, chat_id, text, bot.id, reply_markup)


def claim_next(bot_id):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    with transaction() as db:
        rejected = aliased(Job)
        blocked = db.query(rejected.id).filter(
            rejected.bot_id == Job.bot_id, rejected.chat_id == Job.chat_id,
            rejected.status == 'failed', rejected.error_code == 'destination_unavailable',
            rejected.next_attempt_at > datetime.utcnow()).exists()
        row = db.query(Job).filter(Job.bot_id == bot_id, Job.status == 'queued',
                                   Job.next_attempt_at <= datetime.utcnow(), ~blocked).order_by(Job.id.asc()).with_for_update().first()
        if not row:
            return None
        if row.category in ('subscription','restock','order_progress') and str(row.chat_id).isdigit():
            from services.customer_tools import allows
            if not allows(db, int(row.chat_id), row.category):
                row.status, row.error_code = 'resolved', 'preference_muted'
                return {'skipped': True}
        if bot_id==main_bot_id() and row.category != 'admin' and str(row.chat_id).isdigit() and int(row.chat_id)>0:
            # The shop's private alerts live in Notifications, not extra chat cards.
            row.status, row.error_code = 'inbox', None
            return {'skipped':True,'inbox_user':int(row.chat_id)}
        attempts = row.attempts + 1
        claimed = db.query(Job).filter(Job.id == row.id, Job.status == 'queued').update(
            {Job.status: 'sending', Job.claimed_at: datetime.utcnow(), Job.attempts: attempts}, synchronize_session=False)
        if claimed != 1:
            return None
        return dict(id=row.id, chat_id=row.chat_id, body=row.body, attempts=attempts, reply_markup=row.reply_markup)


def finish(job_id, bot_id, status, message_id=None, error_code=None, delay=0):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    with transaction() as db:
        row = db.query(Job).filter(Job.id == job_id, Job.bot_id == bot_id).with_for_update().first()
        if not row or row.status not in ('sending', 'uncertain'):
            return
        row.status, row.message_id, row.error_code = status, message_id, error_code
        row.sent_at = datetime.utcnow() if status == 'sent' else None
        row.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
        if status == 'sent' and row.source_order_id:
            from models.order import Order
            order = db.get(Order, row.source_order_id)
            if order:
                order.admin_notification_sent_at = row.sent_at


def recover_stale(bot_id):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    with transaction() as db:
        db.query(Job).filter(Job.bot_id == bot_id, Job.status == 'sending',
                             Job.claimed_at < datetime.utcnow()-timedelta(minutes=5)).update(
            {Job.status: 'uncertain', Job.error_code: 'interrupted_send'}, synchronize_session=False)


def retry_failed(job_id, bot_id, admin_id):
    from config import ADMIN_IDS
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    with transaction() as db:
        row = db.query(Job).filter(Job.id == job_id, Job.bot_id == bot_id).with_for_update().first()
        if not row or row.status != 'failed':
            return 'Only a known failed send can be retried. Sent or uncertain sends are protected.'
        if row.attempts >= 10:
            return 'Retry limit reached. Correct the destination or bot configuration first.'
        row.status, row.next_attempt_at = 'queued', datetime.utcnow()
        row.error_code = None
        from models.admin_audit_log import AdminAuditLog
        db.add(AdminAuditLog(admin_telegram_id=admin_id, action='retry_notification', entity_type='notification',
                             entity_id=str(job_id), summary='Explicitly rejected send queued for retry.'))
    return None


def resolve_uncertain(job_id, bot_id, admin_id):
    from config import ADMIN_IDS
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    from models.admin_audit_log import AdminAuditLog
    if admin_id not in ADMIN_IDS:
        return 'Access denied.'
    with transaction() as db:
        row = db.query(Job).filter(Job.id == job_id, Job.bot_id == bot_id).with_for_update().first()
        if not row or row.status != 'uncertain':
            return 'Only an uncertain notification can be resolved this way.'
        row.status, row.error_code = 'resolved', 'admin_reviewed_without_resending'
        db.add(AdminAuditLog(admin_telegram_id=admin_id, action='resolve_notification', entity_type='notification',
                             entity_id=str(job_id), summary='Uncertain send reviewed and closed without resending.'))
    return None


def report(bot_id, page=0):
    from database import SessionLocal
    from models.bot_operations import NotificationJob as Job
    with SessionLocal() as db:
        counts = dict(db.query(Job.status, func.count(Job.id)).filter(Job.bot_id == bot_id).group_by(Job.status).all())
        rows = db.query(Job).filter(Job.bot_id == bot_id).order_by(Job.id.desc()).offset(max(0,page)*8).limit(8).all()
        return counts, [dict(id=r.id, status=r.status, chat_id=r.chat_id, attempts=r.attempts,
                             error=r.error_code, sent_at=r.sent_at) for r in rows]


async def send_one(bot):
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
    job = await asyncio.to_thread(claim_next, bot.id)
    if not job:
        return False
    if job.get('skipped'):
        if job.get('inbox_user'):
            from services.notification_inbox import refresh_home_badge
            await refresh_home_badge(bot,job['inbox_user'])
        return True
    try:
        from aiogram.types import InlineKeyboardMarkup
        markup = InlineKeyboardMarkup.model_validate(job['reply_markup']) if job.get('reply_markup') else None
    except ValueError:
        await asyncio.to_thread(finish, job['id'], bot.id, 'failed', None, 'invalid_keyboard')
        return True
    try:
        message = await bot.send_message(job['chat_id'], job['body'], parse_mode='HTML', reply_markup=markup)
    except TelegramRetryAfter as error:
        await asyncio.to_thread(finish, job['id'], bot.id, 'queued' if job['attempts'] < 5 else 'failed',
                                None, 'telegram_rate_limit', max(1, int(error.retry_after)))
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        # These are explicit rejections, so a corrected request may be safely retried.
        unavailable = isinstance(error, TelegramForbiddenError) or any(s in str(error).lower() for s in
                          ('chat not found', 'bot was kicked', 'not enough rights', 'bot is not a member'))
        await asyncio.to_thread(finish, job['id'], bot.id, 'failed', None,
                                'destination_unavailable' if unavailable else type(error).__name__, 300 if unavailable else 0)
    except asyncio.CancelledError:
        # Leave the durable claim intact; recovery marks it uncertain.
        raise
    except Exception:
        await asyncio.to_thread(finish, job['id'], bot.id, 'uncertain', None, 'delivery_not_confirmed')
    else:
        # If this DB write fails, do not send again: recovery holds the claim for review.
        await asyncio.to_thread(finish, job['id'], bot.id, 'sent', message.message_id)
    return True


async def notification_loop(bot):
    from services.provider_health import flush_health
    from time import monotonic
    maintenance_at = reminders_at = 0
    while True:
        try:
            now = monotonic()
            if now >= maintenance_at:
                await asyncio.to_thread(reroute_pending_admin_alerts, bot.id)
                await asyncio.to_thread(recover_stale, bot.id)
                await asyncio.to_thread(flush_health)
                maintenance_at = now+10
            if now >= reminders_at:
                from services.customer_tools import subscription_reminders
                await asyncio.to_thread(subscription_reminders)
                reminders_at = now+60
            # Bound each tick; a broken recipient must not hold up other jobs.
            for _ in range(10):
                if not await send_one(bot):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Notification/health service unavailable; will retry (no message bodies logged)')
        await asyncio.sleep(1)
