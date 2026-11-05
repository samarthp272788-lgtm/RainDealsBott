"""Isolated async regression checks: no config, credentials, DB, or network."""
import ast
import asyncio
import logging
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'handlers/products.py').read_text(encoding='utf-8'))


def load_functions(names, namespace):
    nodes = [node for node in TREE.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'handlers/products.py'), 'exec'), namespace)


class CatalogRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = dict(asyncio=asyncio, time=time, logger=Mock(spec=logging.Logger),
                       ResellerManager=Mock(), _reseller_cache_lock=asyncio.Lock(),
                       _reseller_stock_cache={}, _reseller_stock_cache_time=0,
                       _reseller_refresh_task=None, RESELLER_CACHE_TTL=30,
                       RESELLER_STOCK_TIMEOUT_SECONDS=0.01,
                       _load_stock_refresh_providers=Mock(return_value=[
                           {'id': 1, 'name': 'test', 'api_key': 'fixture', 'base_url': 'https://example.invalid'}]))
        load_functions({'_refresh_reseller_stock_cache_if_needed', '_schedule_reseller_stock_refresh'}, self.ns)

    async def asyncTearDown(self):
        task = self.ns['_reseller_refresh_task']
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_clicks_share_one_background_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(_manager):
            entered.set()
            await release.wait()
            return [{'id': 'service-1', 'stock': 7}]
        self.ns['_call_reseller_get_products'] = AsyncMock(side_effect=blocked)
        for _ in range(25):
            self.ns['_schedule_reseller_stock_refresh']()
        task = self.ns['_reseller_refresh_task']
        await asyncio.wait_for(entered.wait(), 0.5)
        self.assertFalse(task.done())  # Scheduler returned while supplier was blocked.
        self.assertEqual(self.ns['_call_reseller_get_products'].await_count, 1)
        for _ in range(25):
            self.ns['_schedule_reseller_stock_refresh']()
            self.assertIs(task, self.ns['_reseller_refresh_task'])
        release.set()
        await task
        self.assertEqual(self.ns['_reseller_stock_cache']['service-1'], 7)

    async def test_failed_empty_refresh_is_cached_for_waiting_callers(self):
        self.ns['_call_reseller_get_products'] = AsyncMock(side_effect=TimeoutError())
        await asyncio.gather(*(self.ns['_refresh_reseller_stock_cache_if_needed']() for _ in range(20)))
        self.assertEqual(self.ns['_call_reseller_get_products'].await_count, 1)
        self.assertEqual(self.ns['_reseller_stock_cache'], {})
        self.ns['_schedule_reseller_stock_refresh']()
        self.assertIsNone(self.ns['_reseller_refresh_task'])

    async def test_timeout_preserves_previous_stock(self):
        self.ns['_reseller_stock_cache']['existing'] = 3
        self.ns['_call_reseller_get_products'] = AsyncMock(side_effect=lambda _manager: None)
        async def hanging(_manager):
            await asyncio.Event().wait()
        self.ns['_call_reseller_get_products'].side_effect = hanging
        await asyncio.wait_for(self.ns['_refresh_reseller_stock_cache_if_needed'](), 2.5)
        self.assertEqual(self.ns['_reseller_stock_cache'], {'existing': 3})
        self.assertGreater(self.ns['_reseller_stock_cache_time'], 0)

    async def test_provider_lookup_runs_outside_event_loop_thread(self):
        main_thread = threading.get_ident()
        self.ns['_load_stock_refresh_providers'].side_effect = lambda: self.assertNotEqual(threading.get_ident(), main_thread) or []
        await self.ns['_refresh_reseller_stock_cache_if_needed']()

    async def test_unreachable_notification_chat_backs_off_without_blocking_others(self):
        namespace = dict(asyncio=asyncio, time=time, logger=Mock(spec=logging.Logger),
                         _stock_destination_retry_at={}, _stock_bot_username=None)
        load_functions({'_deliver_stock_channel_message'}, namespace)
        async def send(target, *_args, **_kwargs):
            if target == 'missing':
                raise RuntimeError('Bad Request: chat not found')
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        for _ in range(2):
            await namespace['_deliver_stock_channel_message'](bot, 'stock', ['missing', 'valid'], None, False)
        targets = [call.args[0] for call in bot.send_message.call_args_list]
        self.assertEqual(targets.count('missing'), 1)
        self.assertEqual(targets.count('valid'), 2)

    def test_only_purchase_can_wait_for_supplier_stock(self):
        waiters = []
        for node in TREE.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Await) and isinstance(child.value, ast.Call) and isinstance(child.value.func, ast.Name):
                    if child.value.func.id == '_refresh_reseller_stock_cache_if_needed':
                        waiters.append(node.name)
        self.assertEqual(waiters, ['_do_reseller_purchase'])

    def test_health_routes_accept_head_probes(self):
        tree = ast.parse((ROOT / 'render_api.py').read_text(encoding='utf-8'))
        for name in ('root', 'health'):
            node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            route = next(d for d in node.decorator_list if isinstance(d, ast.Call))
            methods = next(ast.literal_eval(k.value) for k in route.keywords if k.arg == 'methods')
            self.assertEqual(set(methods), {'GET', 'HEAD'})


if __name__ == '__main__':
    unittest.main()
