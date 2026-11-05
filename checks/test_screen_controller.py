import asyncio
from datetime import datetime, timezone
import unittest
from aiogram.types import CallbackQuery, Chat, Message, User
from middleware.screen_navigation import ScreenNavigationMiddleware
from services.screen_controller import managed_screen, screens, current_screen


def event(data, user=7):
    return CallbackQuery(id=str(user), from_user=User(id=user, is_bot=False, first_name='Test'),
                         chat_instance='test', data=data,
                         message=Message(message_id=1, date=datetime.now(timezone.utc),
                                         chat=Chat(id=user, type='private')))


class State:
    def __init__(self):
        self.data = {}
    async def update_data(self, **values):
        self.data.update(values)


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        screens.clear()

    async def test_slow_product_cannot_replace_home_or_its_input_state(self):
        middleware, state = ScreenNavigationMiddleware(), State()
        entered, release = asyncio.Event(), asyncio.Event()
        rendered = []
        @managed_screen
        async def render(target, text, guarded):
            await guarded.update_data(view=text)  # Nested operation must not deadlock.
            rendered.append(text)
        async def slow(e, data):
            entered.set()
            await release.wait()
            await data['state'].update_data(view='old')
            await render(e, 'old', data['state'])
        async def home(e, data):
            await render(e, 'home', data['state'])
        task = asyncio.create_task(middleware(slow, event('product_1'), {'state': state}))
        await entered.wait()
        await middleware(home, event('main_menu'), {'state': state})
        release.set()
        await task
        self.assertEqual(rendered, ['home'])
        self.assertEqual(state.data['view'], 'home')
        self.assertIsNone(current_screen.get())

    async def test_old_render_is_rejected_even_without_state_mutation(self):
        middleware = ScreenNavigationMiddleware()
        entered, release = asyncio.Event(), asyncio.Event()
        rendered = []
        @managed_screen
        async def render(target):
            rendered.append(target.data)
        async def old(e, data):
            entered.set()
            await release.wait()
            await render(e)
        task = asyncio.create_task(middleware(old, event('products_menu'), {}))
        await entered.wait()
        await middleware(lambda e, data: render(e), event('orders_menu'), {})
        release.set()
        await task
        self.assertEqual(rendered, ['orders_menu'])

    async def test_other_user_does_not_cancel_an_inflight_screen(self):
        middleware = ScreenNavigationMiddleware()
        entered, release = asyncio.Event(), asyncio.Event()
        rendered = []
        @managed_screen
        async def render(e):
            rendered.append(e.from_user.id)
        async def old(e, data):
            entered.set()
            await release.wait()
            await render(e)
        task = asyncio.create_task(middleware(old, event('product_1', 7), {}))
        await entered.wait()
        await middleware(lambda e, data: render(e), event('main_menu', 8), {})
        release.set()
        await task
        self.assertEqual(rendered, [8, 7])

    async def test_inflight_storage_write_finishes_before_newer_write(self):
        middleware = ScreenNavigationMiddleware()
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowState(State):
            async def update_data(self, **values):
                if values['view'] == 'old':
                    entered.set()
                    await release.wait()
                self.data.update(values)
        state = SlowState()
        async def handler(e, data):
            await data['state'].update_data(view='old' if e.data == 'product_1' else 'home')
        old = asyncio.create_task(middleware(handler, event('product_1'), {'state': state}))
        await entered.wait()
        new = asyncio.create_task(middleware(handler, event('main_menu'), {'state': state}))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(old, new)
        self.assertEqual(state.data['view'], 'home')
