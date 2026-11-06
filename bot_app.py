# ============================================================
# NOMANBOT - BOT APPLICATION
# ============================================================

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from config import BOT_TOKEN

from middleware.membership import BannedUserMiddleware
from middleware.session_timeout import CallbackSessionTimeoutMiddleware
from middleware.screen_navigation import ScreenNavigationMiddleware
from services.performance import TelegramTimings, UpdateTimings


# ============================================================
# USER ROUTERS
# ============================================================

from handlers.start import router as start_router
from handlers.restart import router as restart_router
from handlers.order_workflow import router as order_workflow_router
from handlers.bot_operations import router as bot_operations_router
from handlers.products import _fetch_active_products, router as products_router
from handlers.orders import router as orders_router
from handlers.deposit import router as deposit_router
from handlers.referral import router as referrals_router
from handlers.support import router as support_router
from handlers.user_promo import router as user_promo_router
from handlers.api_keys import router as api_keys_router
from handlers.commands import router as commands_router


# ============================================================
# ADMIN ROUTERS
# ============================================================

from handlers.admin import router as admin_router
from handlers.admin_products import router as admin_products_router
from handlers.admin_product_manage import router as admin_product_manage_router
from handlers.admin_orders import router as admin_orders_router
from handlers.admin_deposits import router as admin_deposits_router
from handlers.admin_support import router as admin_support_router
from handlers.admin_promo import router as admin_promo_router


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# BOT TOKEN
# ============================================================

if not BOT_TOKEN:

    raise RuntimeError(
        "BOT_TOKEN is not configured."
    )


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)


# ============================================================
# DISPATCHER
# ============================================================

bot.session.middleware(TelegramTimings())
from services.session_resume import DraftPromptRecorder
bot.session.middleware(DraftPromptRecorder())


dp = Dispatcher(
    storage=MemoryStorage(),
)


async def _register_bot_commands(bot: Bot):
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Open the home menu"),
                BotCommand(command="restart", description="Continue your last step"),
                BotCommand(command="shop", description="Browse the shop"),
                BotCommand(command="wallet", description="View your wallet and balance"),
                BotCommand(command="orders", description="View your orders"),
                BotCommand(command="profile", description="View your profile"),
            ],
            scope=BotCommandScopeDefault(),
        )
    except Exception:
        logger.exception("Could not register Telegram bot commands")


async def _warm_product_catalog(bot: Bot):
    try:
        from services.order_requirements_schema import ensure_order_requirements_schema
        import asyncio
        await asyncio.to_thread(ensure_order_requirements_schema)
        await _fetch_active_products()
        logger.info("Product catalog cache warmed")
    except Exception:
        logger.exception("Could not warm product catalog cache")


dp.startup.register(_register_bot_commands)
dp.startup.register(_warm_product_catalog)


async def _flush_customer_drafts(bot: Bot):
    from services.session_resume import flush_pending_resume
    await flush_pending_resume()


dp.shutdown.register(_flush_customer_drafts)


# ============================================================
# GLOBAL BAN MIDDLEWARE
# ============================================================

dp.update.outer_middleware(
    BannedUserMiddleware()
)
session_timeout_middleware = CallbackSessionTimeoutMiddleware()
dp.callback_query.outer_middleware(UpdateTimings())
dp.message.outer_middleware(UpdateTimings())
screen_navigation_middleware = ScreenNavigationMiddleware()
dp.callback_query.outer_middleware(screen_navigation_middleware)
dp.message.outer_middleware(screen_navigation_middleware)
dp.callback_query.outer_middleware(session_timeout_middleware)
dp.message.outer_middleware(session_timeout_middleware)


# ============================================================
# LOAD USER ROUTERS
# ============================================================

logger.info(
    "Loading user routers..."
)

dp.include_router(
    start_router
)
dp.include_router(restart_router)
dp.include_router(order_workflow_router)
dp.include_router(bot_operations_router)

dp.include_router(
    products_router
)

dp.include_router(
    orders_router
)

dp.include_router(
    referrals_router
)

dp.include_router(
    deposit_router
)

dp.include_router(
    user_promo_router
)

# API Access button handler
dp.include_router(
    api_keys_router
)

dp.include_router(
    support_router
)

dp.include_router(
    commands_router
)


# ============================================================
# LOAD ADMIN ROUTERS
# ============================================================

logger.info(
    "Loading admin routers..."
)

dp.include_router(
    admin_router
)

dp.include_router(
    admin_products_router
)

dp.include_router(
    admin_product_manage_router
)

dp.include_router(
    admin_orders_router
)

dp.include_router(
    admin_deposits_router
)

dp.include_router(
    admin_support_router
)

dp.include_router(
    admin_promo_router
)


# ============================================================
# READY
# ============================================================

logger.info(
    "=================================================="
)

logger.info(
    "NomanBot initialized successfully"
)

logger.info(
    "All routers loaded"
)

logger.info(
    "=================================================="
)


# ============================================================
# BOT INFO
# ============================================================

async def get_bot_info():

    return await bot.get_me()


# ============================================================
# CLOSE BOT
# ============================================================

async def close_bot():

    try:

        await bot.session.close()

        logger.info(
            "Bot session closed"
        )

    except Exception:

        logger.exception(
            "Error closing bot session"
        )


# ============================================================
# EXPORTS
# ============================================================

__all__ = [
    "bot",
    "dp",
    "get_bot_info",
    "close_bot",
]
