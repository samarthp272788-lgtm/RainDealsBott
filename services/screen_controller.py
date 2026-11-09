"""One card registry and render lock; older navigation cannot repaint a new view."""
import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps


class SupersededScreen(Exception):
    pass


@dataclass
class Screen:
    version: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: int = 0
    owner: object = None
    prompt: dict | None = None


screens = OrderedDict()
locations = OrderedDict()
current_screen = ContextVar('current_screen', default=None)
MAX_SCREENS = 10000


def get_screen(key):
    if key not in screens:
        if len(screens) >= MAX_SCREENS:
            removed = False
            for old_key, old in list(screens.items()):
                if not old.active and not old.lock.locked():
                    screens.pop(old_key)
                    removed = True
                    break
            if not removed:
                # All entries are busy. Evict the least-recent one anyway;
                # its handler will fail assert_current() before repainting.
                screens.popitem(last=False)
        screens[key] = Screen()
    screens.move_to_end(key)
    return screens[key]


def assert_current():
    current = current_screen.get()
    if current:
        key, entry, version = current
        if screens.get(key) is not entry or entry.version != version:
            raise SupersededScreen()


@asynccontextmanager
async def screen_operation():
    current = current_screen.get()
    assert_current()
    if not current:
        yield
        return
    entry = current[1]
    task = asyncio.current_task()
    if entry.owner is task:
        yield
        return
    async with entry.lock:
        assert_current()
        entry.owner = task
        try:
            yield
        finally:
            entry.owner = None


def managed_screen(fn):
    @wraps(fn)
    async def wrapped(target, *args, **kwargs):
        async with screen_operation():
            return await fn(target, *args, **kwargs)
    return wrapped


class GuardedState:
    """Keep a slow handler from changing a newer screen's input state."""
    def __init__(self, state):
        self._state = state
    def __getattr__(self, name):
        return getattr(self._state, name)
    async def get_state(self):
        assert_current()
        value = await self._state.get_state()
        assert_current()
        return value
    async def get_data(self):
        assert_current()
        value = await self._state.get_data()
        assert_current()
        return value
    async def set_state(self, value):
        async with screen_operation():
            return await self._state.set_state(value)
    async def set_data(self, value):
        async with screen_operation():
            return await self._state.set_data(value)
    async def update_data(self, *args, **kwargs):
        async with screen_operation():
            return await self._state.update_data(*args, **kwargs)
    async def clear(self):
        async with screen_operation():
            return await self._state.clear()
