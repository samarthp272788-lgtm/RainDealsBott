"""Bounded, process-local latency aggregates. Never retain SQL or payloads."""
from collections import OrderedDict, deque
from functools import wraps
from contextvars import ContextVar
import threading
import time

_metrics = OrderedDict()
_lock = threading.Lock()
operation = ContextVar('latency_operation', default='background')


def observe(category, label, elapsed, failed=False):
    key = (category, str(label)[:80])
    with _lock:
        if key not in _metrics:
            if len(_metrics) >= 200:
                _metrics.popitem(last=False)
            _metrics[key] = dict(count=0, failed=0, total=0, samples=deque(maxlen=200))
        metric = _metrics[key]
        metric['count'] += 1
        metric['failed'] += int(failed)
        metric['total'] += elapsed
        metric['samples'].append(elapsed)


def snapshot():
    with _lock:
        result = []
        for (category, label), metric in _metrics.items():
            samples = sorted(metric['samples'])
            result.append(dict(category=category, label=label, count=metric['count'], failed=metric['failed'],
                               average_ms=round(metric['total']/metric['count']*1000, 1),
                               p95_ms=round(samples[min(len(samples)-1, int(len(samples)*.95))]*1000, 1)))
        return sorted(result, key=lambda r: r['p95_ms'], reverse=True)


def supplier_observed(fn):
    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        from services.provider_health import observe_request
        started, error = time.perf_counter(), None
        try:
            return await fn(self, *args, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            feature = kwargs.get('feature_name', 'operation')
            # Labels are fixed operation names, never provider URLs, headers or bodies.
            feature = feature if feature in {'products', 'balance', 'order', 'orders', 'order_details', 'stats'} else 'operation'
            observe('supplier', feature, time.perf_counter()-started, error is not None)
            observe_request(self.provider_id, error)
    return wrapped


def install_database_observers(engine):
    from sqlalchemy import event
    if getattr(engine, '_bot_latency_observer', False):
        return
    engine._bot_latency_observer = True
    @event.listens_for(engine, 'before_cursor_execute')
    def before(conn, cursor, statement, parameters, context, executemany):
        context._bot_started = time.perf_counter()
    @event.listens_for(engine, 'after_cursor_execute')
    def after(conn, cursor, statement, parameters, context, executemany):
        observe('database', operation.get(), time.perf_counter()-context._bot_started)
    @event.listens_for(engine, 'handle_error')
    def failure(context):
        execution = context.execution_context
        if execution is not None and hasattr(execution, '_bot_started'):
            observe('database', operation.get(), time.perf_counter()-execution._bot_started, True)

    # SQL execution events start only after a pooled connection is checked
    # out. Wrap the public pool.connect entry point to include queue wait and
    # connection creation time in the latency totals.
    pool = engine.pool
    if not getattr(pool, '_bot_pool_wait_observer', False):
        original_connect = pool.connect

        def timed_pool_connect(*args, **kwargs):
            started = time.perf_counter()
            failed = False
            try:
                return original_connect(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                observe('database_pool', 'checkout_wait', time.perf_counter() - started, failed)

        try:
            pool.connect = timed_pool_connect
            pool._bot_pool_wait_observer = True
        except Exception:
            # Metrics must never prevent database startup if an alternate
            # SQLAlchemy pool implementation makes the method read-only.
            pass


class TelegramTimings:
    async def __call__(self, make_request, bot, method):
        started, failed = time.perf_counter(), False
        try:
            return await make_request(bot, method)
        except BaseException:
            failed = True
            raise
        finally:
            if type(method).__name__ != 'GetUpdates':
                observe('telegram', type(method).__name__, time.perf_counter()-started, failed)


class UpdateTimings:
    async def __call__(self, handler, event, data):
        started, failed = time.perf_counter(), False
        value = getattr(event, 'data', '') or ''
        prefixes = ('products_cat_', 'products_', 'product_', 'order_', 'deposit_', 'admin_', 'warranty_', 'ops_')
        label = next((prefix.rstrip('_') for prefix in prefixes if value.startswith(prefix)), 'callback') if value else 'message'
        token = operation.set(label)
        # Never use callback text or a user's input as a diagnostic label.
        try:
            return await handler(event, data)
        except BaseException:
            failed = True
            raise
        finally:
            observe('update', label, time.perf_counter()-started, failed)
            operation.reset(token)
