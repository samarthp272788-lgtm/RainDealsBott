"""Restart long-running service coroutines that fail or return unexpectedly."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def supervise(name: str, factory: Callable[[], Awaitable[object]], retry_seconds: float = 5) -> None:
    while True:
        try:
            await factory()
            logger.error("Background service %s exited; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background service %s failed; restarting", name)
        await asyncio.sleep(max(1, retry_seconds))
