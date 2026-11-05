"""Save resumable customer drafts, never purchases or admin/password flows."""
import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
import json
import logging
import re

logger = logging.getLogger(__name__)
_cache = OrderedDict()
_pending = {}
_writers = {}
MAX_AGE_SECONDS = 86400
SAFE_STATES = {'DepositState:waiting_network', 'DepositState:waiting_amount',
               'DepositState:waiting_txid', 'DepositState:waiting_promo_code',
               'DepositState:waiting_optional_promo', 'PurchaseStates:waiting_qty',
               'SearchStates:waiting_query', 'OrderRequirementStates:waiting_input',
               'SupportState:waiting_message', 'SupportState:tracking_ticket', 'OrderFilterStates:waiting_filter'}
SAFE_NAV = re.compile(r'^(products_menu|products_all|products_cat_\d+|product_\d+|orders_menu|order_detail_\d+|deposit_start)$')
SAFE_KEYS = {'deposit_id', 'network', 'invoice_code', 'invoice_expires_at', 'amount_usdt',
             'pending_product_id', '_card_chat_id', '_card_message_id',
             'order_requirement_order_id', 'order_requirement_fields',
             'order_requirement_index', 'order_requirement_values', 'ticket_category', 'ticket_priority'}


def make_snapshot(state, data, navigation=None):
    if state and state not in SAFE_STATES:
        return None
    navigation = navigation if navigation and SAFE_NAV.fullmatch(navigation) else None
    if not state and not navigation:
        return None
    clean = {k: v for k, v in data.items() if k in SAFE_KEYS or re.fullmatch(r'(?:qty|checkout_token)_\d+', k)}
    return json.loads(json.dumps({'state': state, 'data': clean, 'navigation': navigation}, default=str))


def _persist(key, snapshot):
    from database import SessionLocal
    from models.bot_resume_session import BotResumeSession
    db = SessionLocal()
    try:
        row = db.get(BotResumeSession, key)
        if snapshot is None:
            if row:
                db.delete(row)
        else:
            if row is None:
                row = BotResumeSession(telegram_id=key[0], chat_id=key[1])
                db.add(row)
            row.snapshot = snapshot
            row.expires_at = datetime.utcnow() + timedelta(seconds=MAX_AGE_SECONDS)
        db.commit()
    finally:
        db.close()


async def _flush(key):
    while key in _pending:
        await asyncio.sleep(0.15)  # Coalesce rapid navigation without holding handlers.
        snapshot = _pending.pop(key)
        try:
            await asyncio.to_thread(_persist, key, snapshot)
        except Exception:
            logger.exception('Could not persist resumable customer session for %s', key[0])


def save_resume(key, snapshot):
    cached = _cache.get(key)
    if cached and cached[1] == snapshot and (datetime.utcnow() - cached[0]).total_seconds() < 60:
        return
    _cache[key] = (datetime.utcnow(), snapshot)
    _cache.move_to_end(key)
    while len(_cache) > 10000:
        _cache.popitem(last=False)
    _pending[key] = snapshot
    if key not in _writers or _writers[key].done():
        task = asyncio.create_task(_flush(key))
        _writers[key] = task
        task.add_done_callback(lambda done: _writers.pop(key, None) if _writers.get(key) is done else None)


def _load(key):
    from database import SessionLocal
    from models.bot_resume_session import BotResumeSession
    db = SessionLocal()
    try:
        row = db.get(BotResumeSession, key)
        return row.snapshot if row and row.expires_at > datetime.utcnow() else None
    finally:
        db.close()


async def load_resume(key):
    if key in _cache:
        saved_at, snapshot = _cache[key]
        return snapshot if (datetime.utcnow() - saved_at).total_seconds() < MAX_AGE_SECONDS else None
    return await asyncio.to_thread(_load, key)


async def flush_pending_resume():
    tasks = list(_writers.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
