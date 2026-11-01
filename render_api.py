"""Public FastAPI entry point.

Runs:
- Main Telegram bot polling (unless the main bot uses Cloudflare transport)
- Cloudflare delivery-bot bridge caller
- Deposit checker
- Public reseller API

Start:
    python render_api.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.reseller_v1 import router as reseller_router
from bot_app import bot, dp
from services.deposit_checker import deposit_checker_loop
from services.order_notifications import order_notification_loop
from services.product_api_access import ensure_product_api_access_schema
from services.provider_schema import ensure_provider_schema
from services.maintenance_mode import ensure_maintenance_schema, is_maintenance_enabled
from services.order_rating_schema import ensure_order_rating_schema
from services.upstream_callbacks import process_upstream_callback
from services.cloudflare_bridge import dispatch_update, ensure_schema, verify_request
from config import API_MAX_REQUEST_BYTES


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("render_api")


# ============================================================
# ENVIRONMENT
# ============================================================

PORT = int(os.getenv("PORT", "10000"))

INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "").strip()
CLOUDFLARE_BRIDGE_SECRET = os.getenv("CLOUDFLARE_BRIDGE_SECRET", "").strip()
TELEGRAM_TRANSPORT = os.getenv("TELEGRAM_TRANSPORT", "polling").strip().lower()
MAX_BRIDGE_BODY_BYTES = 256_000

if not INTERNAL_SECRET:
    logger.warning(
        "INTERNAL_API_SECRET is not configured. "
        "The reseller API will reject requests."
    )


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    deposit_task: asyncio.Task | None = None
    order_notification_task: asyncio.Task | None = None
    polling_task: asyncio.Task | None = None

    try:
        await asyncio.to_thread(ensure_product_api_access_schema)
        await asyncio.to_thread(ensure_provider_schema)
        await asyncio.to_thread(ensure_maintenance_schema)
        await asyncio.to_thread(ensure_order_rating_schema)

        if TELEGRAM_TRANSPORT == "cloudflare":
            if len(CLOUDFLARE_BRIDGE_SECRET) < 32:
                raise RuntimeError(
                    "CLOUDFLARE_BRIDGE_SECRET must contain at least 32 characters "
                    "when TELEGRAM_TRANSPORT=cloudflare"
                )
            await asyncio.to_thread(ensure_schema)

        # ----------------------------------------------------
        # Telegram main bot
        # ----------------------------------------------------

        if TELEGRAM_TRANSPORT != "cloudflare":
            await bot.delete_webhook(drop_pending_updates=True)

        me = await bot.get_me()

        logger.info(
            "Main Telegram bot connected: @%s",
            me.username,
        )

        # ----------------------------------------------------
        # Deposit checker
        # ----------------------------------------------------

        deposit_task = asyncio.create_task(
            deposit_checker_loop(bot),
            name="deposit-checker",
        )

        order_notification_task = asyncio.create_task(
            order_notification_loop(bot),
            name="admin-order-notifications",
        )

        # ----------------------------------------------------
        # Main Telegram polling
        # ----------------------------------------------------

        if TELEGRAM_TRANSPORT != "cloudflare":
            polling_task = asyncio.create_task(
                dp.start_polling(
                    bot,
                    handle_signals=False,
                    close_bot_session=False,
                ),
                name="telegram-polling",
            )
        else:
            logger.info("Main bot polling disabled; waiting for signed Cloudflare queue updates")

        logger.info("Manual delivery bot transport is hosted by the Cloudflare webhook Worker")

        logger.info(
            "FastAPI reseller API + Telegram services started"
        )

        yield

    finally:

        logger.info("Shutting down services...")

        tasks = (
            polling_task,
            deposit_task,
            order_notification_task,
        )

        for task in tasks:
            if task and not task.done():
                task.cancel()

        for task in tasks:
            if task:
                with suppress(asyncio.CancelledError):
                    await task

        if bot.session:
            await bot.session.close()

        logger.info("Shutdown complete")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Rain Reseller API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.post("/internal/cloudflare/telegram/update")
async def cloudflare_telegram_update(request: Request):
    if TELEGRAM_TRANSPORT != "cloudflare":
        return JSONResponse({"error": "bridge_disabled"}, status_code=404)
    if not CLOUDFLARE_BRIDGE_SECRET:
        return JSONResponse({"error": "bridge_not_configured"}, status_code=503)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BRIDGE_BODY_BYTES:
                return JSONResponse({"error": "payload_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)

    raw_body = await request.body()
    if len(raw_body) > MAX_BRIDGE_BODY_BYTES:
        return JSONResponse({"error": "payload_too_large"}, status_code=413)

    if not verify_request(
        CLOUDFLARE_BRIDGE_SECRET,
        request.headers.get("X-RainDeals-Timestamp", ""),
        raw_body,
        request.headers.get("X-RainDeals-Signature", ""),
    ):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    try:
        update_data = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(update_data, dict) or type(update_data.get("update_id")) is not int:
        return JSONResponse({"error": "invalid_telegram_update"}, status_code=400)

    from aiogram.types import Update

    try:
        update = Update.model_validate(update_data, context={"bot": bot})
        state = await dispatch_update(
            update_data["update_id"],
            lambda: dp.feed_update(bot, update),
        )
    except Exception:
        logger.exception("Cloudflare bridge failed to dispatch update %s", update_data["update_id"])
        return JSONResponse({"error": "dispatch_failed"}, status_code=503)

    if state == "busy":
        return JSONResponse({"error": "update_in_progress"}, status_code=503, headers={"Retry-After": "3"})
    return JSONResponse({"ok": True, "state": state}, status_code=200)


@app.post("/api/v1/upstream/callback")
async def upstream_callback(request: Request):
    raw_body = await request.body()
    return await process_upstream_callback(raw_body, request.headers, bot)


@app.exception_handler(RequestValidationError)
async def api_validation_error(request: Request, exc: RequestValidationError):
    """Keep malformed public API requests small and predictable for clients."""

    if request.url.path.startswith("/api/v1/"):
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "invalid_request",
                "message": "Request contains invalid or missing fields.",
            },
        )

    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ============================================================
# REQUEST LOGGING
# ============================================================

@app.middleware("http")
async def request_logger(request: Request, call_next):

    if request.url.path.startswith("/api/v1/") and await asyncio.to_thread(is_maintenance_enabled):
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "error": "maintenance_mode",
                "message": "Rain Store is currently under maintenance. Please check back soon.",
            },
        )

    if request.url.path.startswith("/api/v1/"):
        content_length = request.headers.get("content-length")

        if content_length:
            try:
                request_size = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "error": "invalid_request",
                        "message": "Content-Length must be a valid number.",
                    },
                )

            if request_size < 0 or request_size > API_MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "success": False,
                        "error": "request_too_large",
                        "message": (
                            f"API request body must not exceed "
                            f"{API_MAX_REQUEST_BYTES} bytes."
                        ),
                    },
                )

    logger.info(
        "%s %s",
        request.method,
        request.url.path,
    )

    try:
        response = await call_next(request)

        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
        )

        return response

    except Exception:
        logger.exception(
            "Unhandled request error: %s %s",
            request.method,
            request.url.path,
        )

        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "message": "Internal server error.",
            },
        )


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Rain Reseller API",
        "version": "1.0.0",
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "Rain Reseller API",
    }


# ============================================================
# RESELLER API
# ============================================================

# IMPORTANT:
#
# This is now PUBLIC.
#
# There is no:
#
#     /internal/v1
#
# bridge anymore.
#
# Wasmer/external clients can call the API directly.
#
app.include_router(
    reseller_router,
)


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting server on 0.0.0.0:%s",
        PORT,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
