"""Buffer observations off the request path and persist them in small batches."""
from collections import deque
from datetime import datetime
import threading

_pending = deque(maxlen=2000)
_lock = threading.Lock()


def error_reason(error):
    if not error:
        return None
    code = getattr(error, 'status_code', None)
    try:
        code = int(code) if code else None
    except (ValueError, TypeError):
        code = None
    messages = {401: 'HTTP 401: check provider credentials', 403: 'HTTP 403: access denied',
                404: 'HTTP 404: check endpoint configuration', 429: 'HTTP 429: provider rate limit',
                451: 'HTTP 451: service location restriction', 504: 'HTTP 504: provider timeout'}
    if code in messages:
        return messages[code]
    if code:
        return f'HTTP {int(code)}: provider request failed'
    if isinstance(error, TimeoutError):
        return 'Provider timeout'
    if 'json' in str(error).lower():
        return 'Response is not valid JSON; check the configured endpoint'
    return 'Connection, response format or provider error; check configuration'


def observe_request(key, error=None):
    with _lock:
        _pending.append((str(key)[:100], datetime.utcnow(), 'request', error_reason(error)))


def observe_sync(key, error=None):
    with _lock:
        _pending.append((str(key)[:100], datetime.utcnow(), 'sync', error_reason(error)))


def flush_health():
    from database import transaction
    from models.bot_operations import ProviderHealth
    with _lock:
        batch = list(_pending)
        _pending.clear()
    if not batch:
        return
    try:
        with transaction() as db:
            rows = {}
            for key, at, kind, error in batch:
                if key not in rows:
                    row = db.query(ProviderHealth).filter_by(provider_key=key).with_for_update().first()
                    if not row:
                        row = ProviderHealth(provider_key=key, requests=0, failures=0, consecutive_failures=0)
                        db.add(row)
                    rows[key] = row
                row = rows[key]
                if kind == 'request':
                    row.requests += 1
                    if error:
                        row.failures += 1
                        row.consecutive_failures += 1
                        row.last_failure_at, row.last_error = at, error
                    else:
                        row.last_success_at = at
                        row.consecutive_failures = 0
                elif not error:
                    row.last_sync_at, row.last_error = at, None
                else:
                    row.last_error = error
    except Exception:
        with _lock:
            # Preserve observations after a temporary database failure, within the memory cap.
            newer = list(_pending)
            _pending.clear()
            _pending.extend((batch + newer)[-2000:])
        raise


def dashboard():
    from database import SessionLocal
    from models.provider import Provider
    from models.bot_operations import ProviderHealth
    now = datetime.utcnow()
    with SessionLocal() as db:
        names = {str(p.id): p.name for p in db.query(Provider.id, Provider.name).all()}
        result = []
        for row in db.query(ProviderHealth).all():
            age = int((now-row.last_sync_at).total_seconds()) if row.last_sync_at else None
            result.append(dict(key=row.provider_key, name=names.pop(row.provider_key, row.provider_key),
                               requests=row.requests, failures=row.failures, consecutive=row.consecutive_failures,
                               last_success=row.last_success_at, last_sync=row.last_sync_at,
                               stale=age is None or age > 120, error=row.last_error))
        result.extend(dict(key=k, name=name, requests=0, failures=0, consecutive=0,
                           last_success=None, last_sync=None, stale=True, error='No observations yet') for k, name in names.items())
        return result
