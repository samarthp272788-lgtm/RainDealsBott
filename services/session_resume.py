"""Encrypted input drafts and display state; restoring never executes an action."""
import asyncio
import base64
import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta
import json
import logging
import re
from decimal import Decimal
from contextvars import ContextVar
from contextlib import contextmanager

logger = logging.getLogger(__name__)
_cache = OrderedDict()
_pending = {}
_writers = {}
MAX_AGE_SECONDS = 86400
_capture_prompt = ContextVar('capture_resume_prompt', default=True)
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
ADMIN_FAMILIES = {'AddProduct', 'EditProduct', 'AddStock', 'DeleteProduct', 'AddAccounts',
                  'EditAccounts', 'EditBulkPricing', 'DeliverOrder', 'BroadcastState',
                  'AdminUserBalanceState', 'CustomRateState', 'ProviderSetupState',
                  'ProviderEditState', 'MaintenanceModeState', 'ManagerWebState',
                  'PromoCodeCreation', 'WorkInput'}
EXTRA_USER_STATES = {'CustomerInput:quantity', 'CustomerInput:cancel_reason', 'CustomerInput:claim_reply', 'Inputs:reason', 'Inputs:deactivated', 'Inputs:evidence', 'Inputs:confirmation',
                     'Inputs:product_confirmation','Inputs:issue_type',
                     'RedeemState:waiting_for_code', 'OrderFilterStates:waiting_date_range',
                     'OrderFilterStates:waiting_export_format', 'OrderFilterStates:waiting_rating',
                     'OrderFilterStates:waiting_reminder_time'}


def admin_state(state):
    return bool(state and (state.split(':', 1)[0] in ADMIN_FAMILIES or state in {'Inputs:warranty_days', 'Inputs:resolution','Inputs:metadata', 'CustomerInput:admin_reply', 'CustomerInput:term_days', 'CustomerInput:hold_resolution'}))


@contextmanager
def without_prompt_capture():
    token = _capture_prompt.set(False)
    try:
        yield
    finally:
        _capture_prompt.reset(token)


def is_admin(user_id):
    from config import ADMIN_IDS
    return user_id in ADMIN_IDS


def remember_prompt(user_id, chat_id, text, markup=None, parse_mode=None, photo=None):
    from services.screen_controller import get_screen
    if _capture_prompt.get() and user_id and text:
        if markup is not None and hasattr(markup, 'model_dump'):
            markup = markup.model_dump(mode='json', exclude_none=True)
        get_screen((int(user_id), int(chat_id))).prompt = dict(text=text, reply_markup=markup,
                 parse_mode=parse_mode if isinstance(parse_mode, str) else 'HTML', photo=photo)


class DraftPromptRecorder:
    """Catch direct legacy sends/edits as well as the shared card helpers."""
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        from services.screen_controller import current_screen, assert_current, SupersededScreen
        current = current_screen.get()
        if current and type(method).__name__ in {'SendMessage', 'EditMessageText', 'EditMessageCaption', 'SendPhoto'}:
            try:
                assert_current()
            except SupersededScreen:
                return result
            key = current[0]
            if str(getattr(method, 'chat_id', '')) == str(key[1]):
                text = getattr(method, 'text', None) or getattr(method, 'caption', None)
                photo = getattr(result, 'photo', None)
                remember_prompt(*key, text, getattr(method, 'reply_markup', None),
                                getattr(method, 'parse_mode', None), photo[-1].file_id if photo else None)
        return result


def saved_prompt(key):
    from services.screen_controller import screens
    entry = screens.get(key)
    return entry.prompt if entry else None


def _json_value(value):
    if isinstance(value, Decimal):
        return {'$resume_decimal': str(value)}
    if isinstance(value, datetime):
        return {'$resume_datetime': value.isoformat()}
    from aiogram.types import Message
    if isinstance(value, Message):
        return {'$resume_message': value.model_dump(mode='json', exclude_none=True)}
    raise TypeError('Unsupported draft value: '+type(value).__name__)


def restore_data(data, bot=None):
    def decode(value):
        if isinstance(value, list):
            return [decode(v) for v in value]
        if isinstance(value, dict):
            if set(value) == {'$resume_decimal'}:
                return Decimal(value['$resume_decimal'])
            if set(value) == {'$resume_datetime'}:
                return datetime.fromisoformat(value['$resume_datetime'])
            if set(value) == {'$resume_message'}:
                from aiogram.types import Message
                return Message.model_validate(value['$resume_message']).as_(bot)
            return {k:decode(v) for k,v in value.items()}
        return value
    return decode(data)


def make_snapshot(state, data, navigation=None, *, admin=False, prompt=None, last_activity=None):
    needs_admin = admin_state(state)
    if needs_admin and not admin:
        return None
    if state and state not in SAFE_STATES | EXTRA_USER_STATES and not needs_admin:
        return None
    admin_view = bool(admin and not state and prompt and (not navigation or not SAFE_NAV.fullmatch(navigation)))
    navigation = navigation if navigation and SAFE_NAV.fullmatch(navigation) else None
    if not state and not navigation and not prompt:
        return None
    expanded = needs_admin or state in EXTRA_USER_STATES
    clean = {k:v for k,v in data.items() if (expanded or k in SAFE_KEYS or re.fullmatch(r'(?:qty|checkout_token)_\d+', k))
             and not re.search(r'password|verified|session_token|auth_token|access_granted', k, re.I)}
    value = {'state': state, 'data': clean, 'navigation': navigation}
    if needs_admin or admin_view:
        value['admin'] = True
    if prompt:
        value['prompt'] = prompt
    if last_activity is not None:
        value['last_activity'] = float(last_activity)
    return json.loads(json.dumps(value, default=_json_value))


def _cipher(key):
    from cryptography.fernet import Fernet
    import config
    secret = getattr(config, 'API_KEY_ENCRYPTION_KEY', None) or config.BOT_TOKEN
    seed = f'raindeals-resume-v1:{secret}:{key[0]}:{key[1]}'.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def seal_snapshot(key, snapshot):
    return {'version': 2, 'ciphertext': _cipher(key).encrypt(json.dumps(snapshot).encode()).decode()}


def open_snapshot(key, payload):
    if 'ciphertext' not in payload:
        # Older customer-only rows remain readable; new writes are encrypted.
        return payload if not payload.get('admin') else None
    from cryptography.fernet import InvalidToken
    try:
        return json.loads(_cipher(key).decrypt(payload['ciphertext'].encode(), ttl=MAX_AGE_SECONDS))
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None


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
            row.snapshot = seal_snapshot(key, snapshot)
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
        return open_snapshot(key, row.snapshot) if row and row.expires_at > datetime.utcnow() else None
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
