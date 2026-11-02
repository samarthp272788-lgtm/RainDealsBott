from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


def get_admin_panel():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Users",
                    callback_data="admin_users",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="📦 Products",
                    callback_data="admin_products",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Orders",
                    callback_data="admin_orders",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="💰 Deposits",
                    callback_data="admin_deposits",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Statistics",
                    callback_data="admin_stats",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="🎫 Tickets",
                    callback_data="admin_tickets",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Promocodes",
                    callback_data="admin_promo",   # <-- FIXED
                    style="success"
                ),
                InlineKeyboardButton(
                    text="📢 Broadcast",
                    callback_data="admin_broadcast",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Custom Rates",
                    callback_data="custom_rate_menu",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧾 Admin Activity Log",
                    callback_data="admin_audit",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛠 Maintenance Mode",
                    callback_data="maintenance_mode",
                    style="danger"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Manager Web",
                    callback_data="manager_web_access",
                    style="primary"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="admin_back"
                )
            ]
        ]
    )
