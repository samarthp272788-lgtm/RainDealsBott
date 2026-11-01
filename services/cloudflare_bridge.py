"""Authenticated, deduplicated ingress for Telegram updates from Cloudflare Queues."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from database import engine

logger = logging.getLogger(__name__)

MAX_CLOCK_SKEW_SECONDS = 300
UPDATE_LEASE_SECONDS = 180


def verify_request(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if not secret or len(secret) < 32 or not timestamp or not signature:
        return False
    try:
        signed_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - signed_at) > MAX_CLOCK_SKEW_SECONDS:
        return False
    try:
        message = timestamp.encode("ascii") + b"\n" + raw_body
    except UnicodeEncodeError:
        return False
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def ensure_schema() -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE IF NOT EXISTS cloudflare_bridge_updates ("
            "update_id BIGINT NOT NULL PRIMARY KEY, "
            "status VARCHAR(16) NOT NULL, "
            "lease_until DATETIME(6) NOT NULL, "
            "updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) "
            "ON UPDATE CURRENT_TIMESTAMP(6)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        ))
        connection.execute(text(
            "DELETE FROM cloudflare_bridge_updates "
            "WHERE updated_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 30 DAY)"
        ))


def _claim(update_id: int) -> str:
    with engine.begin() as connection:
        inserted = connection.execute(text(
            "INSERT IGNORE INTO cloudflare_bridge_updates "
            "(update_id, status, lease_until) VALUES "
            "(:update_id, 'processing', "
            f"DATE_ADD(UTC_TIMESTAMP(6), INTERVAL {UPDATE_LEASE_SECONDS} SECOND))"
        ), {"update_id": update_id})
        if inserted.rowcount == 1:
            return "claimed"

        row = connection.execute(text(
            "SELECT status, lease_until > UTC_TIMESTAMP(6) AS lease_active "
            "FROM cloudflare_bridge_updates WHERE update_id=:update_id FOR UPDATE"
        ), {"update_id": update_id}).mappings().first()
        if not row:
            return "busy"
        if row["status"] == "completed":
            return "duplicate"
        if bool(row["lease_active"]):
            return "busy"

        connection.execute(text(
            "UPDATE cloudflare_bridge_updates SET status='processing', "
            f"lease_until=DATE_ADD(UTC_TIMESTAMP(6), INTERVAL {UPDATE_LEASE_SECONDS} SECOND) "
            "WHERE update_id=:update_id"
        ), {"update_id": update_id})
        return "claimed"


def _finish(update_id: int, status: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE cloudflare_bridge_updates SET status=:status, "
            "lease_until=UTC_TIMESTAMP(6) WHERE update_id=:update_id"
        ), {"status": status, "update_id": update_id})


async def dispatch_update(
    update_id: int,
    process: Callable[[], Awaitable[None]],
) -> str:
    started_at = time.perf_counter()
    claim_started_at = started_at
    state = await asyncio.to_thread(_claim, update_id)
    claim_ms = (time.perf_counter() - claim_started_at) * 1000
    if state != "claimed":
        logger.info(
            "cloudflare_update_timing state=%s claim_ms=%.1f total_ms=%.1f",
            state,
            claim_ms,
            (time.perf_counter() - started_at) * 1000,
        )
        return state
    handler_started_at = time.perf_counter()
    try:
        await process()
    except BaseException:
        handler_ms = (time.perf_counter() - handler_started_at) * 1000
        finish_started_at = time.perf_counter()
        await asyncio.to_thread(_finish, update_id, "retry")
        logger.exception(
            "cloudflare_update_timing state=retry claim_ms=%.1f handler_ms=%.1f finish_ms=%.1f total_ms=%.1f",
            claim_ms,
            handler_ms,
            (time.perf_counter() - finish_started_at) * 1000,
            (time.perf_counter() - started_at) * 1000,
        )
        raise
    handler_ms = (time.perf_counter() - handler_started_at) * 1000
    finish_started_at = time.perf_counter()
    await asyncio.to_thread(_finish, update_id, "completed")
    finish_ms = (time.perf_counter() - finish_started_at) * 1000
    logger.info(
        "cloudflare_update_timing state=processed claim_ms=%.1f handler_ms=%.1f finish_ms=%.1f total_ms=%.1f",
        claim_ms,
        handler_ms,
        finish_ms,
        (time.perf_counter() - started_at) * 1000,
    )
    return "processed"
