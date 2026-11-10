"""Ensure only one Render replica polls updates for a given Telegram bot."""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import datetime, timedelta

from database import engine, transaction
from models.bot_operations import BotPollingLease

logger = logging.getLogger(__name__)
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
RETRY_SECONDS = 8


def ensure_polling_lease_schema() -> None:
    try:
        BotPollingLease.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        # Concurrent replicas may both pass checkfirst. Accept only the race
        # where the table now exists; propagate all genuine schema failures.
        from sqlalchemy import inspect
        if not inspect(engine).has_table(BotPollingLease.__tablename__):
            raise


def _acquire(bot_id: int, owner: str) -> bool:
    from sqlalchemy.exc import IntegrityError
    now = datetime.utcnow()
    try:
        with transaction() as db:
            row = db.query(BotPollingLease).filter_by(bot_id=bot_id).with_for_update().first()
            if row is None:
                db.add(BotPollingLease(bot_id=bot_id, owner_token=owner,
                                       lease_until=now + timedelta(seconds=LEASE_SECONDS)))
                db.flush()
                return True
            if row.owner_token != owner and row.lease_until > now:
                return False
            row.owner_token = owner
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            row.updated_at = now
            return True
    except IntegrityError:
        # Another replica inserted the first row after our SELECT.
        return False


def _renew(bot_id: int, owner: str) -> bool:
    now = datetime.utcnow()
    with transaction() as db:
        row = db.query(BotPollingLease).filter_by(bot_id=bot_id).with_for_update().first()
        if not row or row.owner_token != owner or row.lease_until <= now:
            return False
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = now
        return True


def _release(bot_id: int, owner: str) -> None:
    now = datetime.utcnow()
    with transaction() as db:
        row = db.query(BotPollingLease).filter_by(bot_id=bot_id).with_for_update().first()
        if row and row.owner_token == owner:
            row.lease_until = now
            row.updated_at = now


async def run_polling_as_leader(bot, dispatcher) -> None:
    """Poll while holding a renewable DB lease; standby replicas wait safely."""
    bot_id = int(bot.id)
    owner = uuid.uuid4().hex
    while True:
        try:
            acquired = await asyncio.to_thread(_acquire, bot_id, owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram polling lease unavailable; retrying safely")
            await asyncio.sleep(RETRY_SECONDS)
            continue
        if not acquired:
            await asyncio.sleep(RETRY_SECONDS)
            continue

        polling = asyncio.create_task(dispatcher.start_polling(
            bot, handle_signals=False, close_bot_session=False,
        ), name=f"telegram-polling-leader-{bot_id}")
        try:
            while True:
                done, _ = await asyncio.wait({polling}, timeout=HEARTBEAT_SECONDS)
                if done:
                    if polling.cancelled():
                        logger.warning("Telegram polling task was cancelled; restarting after lease release")
                    else:
                        error = polling.exception()
                        if error:
                            logger.error("Telegram polling stopped; restarting after lease release",
                                         exc_info=(type(error), error, error.__traceback__))
                        else:
                            logger.warning("Telegram polling returned; restarting after lease release")
                    break
                try:
                    renewed = await asyncio.to_thread(_renew, bot_id, owner)
                except Exception:
                    logger.exception("Telegram polling lease renewal failed; stopping this poller")
                    renewed = False
                if not renewed:
                    logger.warning("Telegram polling lease was lost; stopping this poller")
                    polling.cancel()
                    with suppress(asyncio.CancelledError):
                        await polling
                    break
        except asyncio.CancelledError:
            polling.cancel()
            with suppress(asyncio.CancelledError):
                await polling
            raise
        finally:
            if not polling.done():
                polling.cancel()
                with suppress(asyncio.CancelledError):
                    await polling
            try:
                await asyncio.to_thread(_release, bot_id, owner)
            except Exception:
                logger.exception("Could not release Telegram polling lease")
        await asyncio.sleep(RETRY_SECONDS)
