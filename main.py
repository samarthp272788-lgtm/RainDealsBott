# ============================================================
# NOMANBOT - RENDER POLLING ENTRY POINT
# ============================================================

import asyncio
import logging
import os

from aiohttp import web

from bot_app import bot, dp

from services.deposit_checker import deposit_checker_loop
from services.deposit_card_recovery import (
    deposit_invoice_expiry_loop,
    recover_interrupted_deposit_cards,
)
from services.order_notifications import order_notification_loop
from services.supplier_reconciliation import supplier_reconciliation_loop
from services.notification_outbox import notification_loop
from services.product_api_access import ensure_product_api_access_schema
from services.provider_schema import ensure_provider_schema
from services.maintenance_mode import ensure_maintenance_schema
from services.order_rating_schema import ensure_order_rating_schema
from services.order_requirements_schema import ensure_order_requirements_schema
from services.warranty_revision_schema import ensure_warranty_revision_schema
from services.task_supervisor import supervise
from services.polling_lease import ensure_polling_lease_schema, run_polling_as_leader
from services.session_resume import flush_pending_resume


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# HEALTH
# ============================================================

async def health(request):
    from services.readiness import database_readiness
    ready = await asyncio.to_thread(database_readiness, True)
    return web.json_response(ready, status=200 if ready['status'] == 'ready' else 503)


# ============================================================
# HTTP SERVER
# ============================================================

async def start_http_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "HTTP server running on port %s",
        port,
    )

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():

    runner = None

    deposit_task = None
    deposit_card_task = None
    notification_outbox_task = None
    order_notification_task = None
    supplier_reconciliation_task = None

    # --------------------------------------------------------
    # HTTP SERVER
    # --------------------------------------------------------

    runner = await start_http_server()

    await asyncio.to_thread(ensure_product_api_access_schema)
    await asyncio.to_thread(ensure_provider_schema)
    await asyncio.to_thread(ensure_maintenance_schema)
    await asyncio.to_thread(ensure_order_rating_schema)
    await asyncio.to_thread(ensure_order_requirements_schema)
    await asyncio.to_thread(ensure_warranty_revision_schema)
    await asyncio.to_thread(ensure_polling_lease_schema)

    # --------------------------------------------------------
    # DEPOSIT CHECKER
    # --------------------------------------------------------

    deposit_task = asyncio.create_task(
        supervise("deposit-checker", lambda: deposit_checker_loop(bot)),
        name="supervisor-deposit-checker",
    )

    logger.info(
        "Deposit checker started"
    )

    notification_outbox_task = asyncio.create_task(
        supervise("notification-outbox", lambda: notification_loop(bot)),
        name="supervisor-notification-outbox",
    )

    order_notification_task = asyncio.create_task(
        supervise("admin-order-notifications", lambda: order_notification_loop(bot), retry_seconds=30),
        name="supervisor-admin-order-notifications",
    )
    supplier_reconciliation_task = asyncio.create_task(
        supervise("supplier-order-reconciliation", lambda: supplier_reconciliation_loop(bot)),
        name="supervisor-supplier-order-reconciliation",
    )

    logger.info(
        "Admin order notification service started"
    )

    # --------------------------------------------------------
    # BOT INFO
    # --------------------------------------------------------

    me = await bot.get_me()

    await recover_interrupted_deposit_cards(bot)

    deposit_card_task = asyncio.create_task(
        supervise("deposit-invoice-card-expiry", lambda: deposit_invoice_expiry_loop(bot)),
        name="supervisor-deposit-invoice-card-expiry",
    )

    logger.info(
        "Logged in as @%s",
        me.username,
    )

    # Preserve Telegram's queued updates on restart; dropping them here can
    # silently lose checkout, deposit-reference, and admin messages.
    await bot.delete_webhook()

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    logger.info(
        "Starting polling..."
    )

    try:

        await run_polling_as_leader(bot, dp)

    finally:

        if notification_outbox_task:
            notification_outbox_task.cancel()
            try:
                await notification_outbox_task
            except asyncio.CancelledError:
                pass

        if deposit_card_task and not deposit_card_task.done():

            deposit_card_task.cancel()

            try:

                await deposit_card_task

            except asyncio.CancelledError:

                pass

        if deposit_task and not deposit_task.done():

            deposit_task.cancel()

            try:

                await deposit_task

            except asyncio.CancelledError:

                pass

        if order_notification_task and not order_notification_task.done():

            order_notification_task.cancel()

            try:

                await order_notification_task

            except asyncio.CancelledError:

                pass

        if supplier_reconciliation_task and not supplier_reconciliation_task.done():
            supplier_reconciliation_task.cancel()
            try:
                await supplier_reconciliation_task
            except asyncio.CancelledError:
                pass

        await flush_pending_resume()

        if runner:

            await runner.cleanup()

            logger.info(
                "HTTP server runner cleaned up"
            )

        if bot and bot.session:

            await bot.session.close()

            logger.info(
                "Bot session closed"
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "NomanBot stopped"
        )

    except Exception as exc:

        logger.exception(
            "FATAL ERROR: %s",
            exc,
        )


