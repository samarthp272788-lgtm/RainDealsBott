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


def enqueue(db, event, chat_id, body, bot_id=None, source_order_id=None, reply_markup=None):
    from models.bot_operations import NotificationJob
    bot_id = bot_id or main_bot_id()
    key = hashlib.sha256(f'{bot_id}:{event}:{chat_id}'.encode()).hexdigest()
    # Callers hold their originating order/request lock. Unique key is an extra backstop.
    if db.query(NotificationJob.id).filter_by(event_key=key).first():
        return
    if reply_markup is not None:
        from aiogram.types import InlineKeyboardMarkup
        reply_markup = InlineKeyboardMarkup.model_validate(reply_markup).model_dump(mode='json', exclude_none=True)
    db.add(NotificationJob(event_key=key, bot_id=bot_id, chat_id=str(chat_id), body=body,
                           status='queued', attempts=0, next_attempt_at=datetime.utcnow(), source_order_id=source_order_id,
                           reply_markup=reply_markup))


def enqueue_once(event, chat_id, body, bot_id=None, reply_markup=None):
    from database import transaction, SessionLocal
    from sqlalchemy.exc import IntegrityError
    from models.bot_operations import NotificationJob
    bot_id = bot_id or main_bot_id()
    try:
        with transaction() as db:
            enqueue(db, event, chat_id, body, bot_id, reply_markup=reply_markup)
    except IntegrityError:
        key = hashlib.sha256(f'{bot_id}:{event}:{chat_id}'.encode()).hexdigest()
        with SessionLocal() as db:
            if not db.query(NotificationJob.id).filter_by(event_key=key).first():
                raise


async def queue_message(bot, event, chat_id, text, reply_markup=None, parse_mode='HTML'):
    if parse_mode not in ('HTML', None):
        raise ValueError('The notification outbox accepts HTML messages only.')
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
    elapsed = 0
    while True:
        try:
            if elapsed % 10 == 0:
                await asyncio.to_thread(recover_stale, bot.id)
                await asyncio.to_thread(flush_health)
            # Bound each tick; a broken recipient must not hold up other jobs.
            for _ in range(10):
                if not await send_one(bot):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning('Notification/health service unavailable; will retry (no message bodies logged)')
        elapsed += 1
        await asyncio.sleep(1)
