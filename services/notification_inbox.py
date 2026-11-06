"""Private notification history shared with the durable notification outbox."""
from datetime import datetime
from sqlalchemy import or_


def _visible(db, user_id, bot_id):
    from models.bot_operations import NotificationJob as Job
    return db.query(Job).filter(Job.bot_id==int(bot_id), Job.chat_id==str(int(user_id)),
        Job.status.in_(('queued','inbox','sent','uncertain','failed')),
        or_(Job.status!='queued', Job.next_attempt_at<=datetime.utcnow()))


def unread_count(db, user_id, bot_id=None):
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    return _visible(db,user_id,bot_id or main_bot_id()).filter(Job.inbox_read_at.is_(None)).count()


def count(user_id,bot_id=None):
    from database import SessionLocal
    with SessionLocal() as db:
        return unread_count(db,user_id,bot_id)


def listing(user_id,page=0,bot_id=None):
    from database import SessionLocal
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    page=max(0,min(int(page),100000))
    with SessionLocal() as db:
        query=_visible(db,user_id,bot_id or main_bot_id())
        rows=query.order_by(Job.id.desc()).offset(page*6).limit(7).all()
        newest=query.order_by(Job.id.desc()).first()
        return dict(unread=query.filter(Job.inbox_read_at.is_(None)).count(),page=page,has_more=len(rows)>6,
                    newest_id=newest.id if newest else 0,
                    rows=[dict(id=r.id,body=r.body,created_at=r.created_at,unread=r.inbox_read_at is None) for r in rows[:6]])


def read(user_id,job_id,bot_id=None):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    with transaction() as db:
        row=_visible(db,user_id,bot_id or main_bot_id()).filter(Job.id==int(job_id)).with_for_update().first()
        if not row:
            raise ValueError('Notification not found.')
        if row.inbox_read_at is None:
            row.inbox_read_at=datetime.utcnow()
        return dict(id=row.id,body=row.body,created_at=row.created_at,reply_markup=row.reply_markup)


def mark_read(user_id,through_id,bot_id=None):
    from database import transaction
    from models.bot_operations import NotificationJob as Job
    from services.notification_outbox import main_bot_id
    through_id=int(through_id)
    if not 0<=through_id<=2**63-1:
        raise ValueError('Invalid notification page.')
    with transaction() as db:
        # Use the page's high-water mark so a concurrent new arrival stays unread.
        return _visible(db,user_id,bot_id or main_bot_id()).filter(Job.id<=through_id,
            Job.inbox_read_at.is_(None)).update({Job.inbox_read_at:datetime.utcnow()},synchronize_session=False)


def badge(unread):
    count=max(0,int(unread))
    label='99+' if count>99 else str(count)
    return '🔔 Notifications'+(f' · 🔴 {label}' if count else '')


async def refresh_home_badge(bot,user_id):
    """Update only a still-visible Home keyboard when its inbox receives an item."""
    import asyncio
    from aiogram.types import InlineKeyboardMarkup
    from services.screen_controller import screens, locations
    key=(int(user_id),int(user_id))
    entry=screens.get(key)
    if not entry or not entry.prompt:
        return
    version=entry.version
    markup=entry.prompt.get('reply_markup') or {}
    if hasattr(markup,'model_dump'):
        markup=markup.model_dump(mode='json',exclude_none=True)
    callbacks={b.get('callback_data') for row in markup.get('inline_keyboard',[]) for b in row}
    if not {'ct_inbox','products_menu','deposit_start'}<=callbacks:
        return
    unread=await asyncio.to_thread(count,user_id,bot.id)
    async with entry.lock:
        if entry.version!=version or screens.get(key) is not entry:
            return
        location=locations.get(key)
        if not location:
            return
        # Copy the markup; never mutate a prompt belonging to an in-flight render.
        markup=InlineKeyboardMarkup.model_validate(markup).model_dump(mode='json',exclude_none=True)
        changed=False
        for row in markup['inline_keyboard']:
            for button in row:
                if button.get('callback_data')=='ct_inbox' and button['text']!=badge(unread):
                    button['text']=badge(unread)
                    changed=True
        if changed:
            try:
                await bot.edit_message_reply_markup(chat_id=location[0],message_id=location[1],
                                                    reply_markup=InlineKeyboardMarkup.model_validate(markup))
            except Exception:
                return  # Deleted history or navigation: Home recomputes the count.
            entry.prompt={**entry.prompt,'reply_markup':markup}
