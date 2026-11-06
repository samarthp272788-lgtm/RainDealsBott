from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 SHOP",
                    callback_data="products_menu",
                    style="primary",
                )
            ],
            [InlineKeyboardButton(text="🛒 Cart", callback_data="ct_cart", style="primary"), InlineKeyboardButton(text="⚙ Preferences", callback_data="ct_preferences")],
            [
                InlineKeyboardButton(
                    text="💳 Wallet",
                    callback_data="deposit_start",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="🎁 Freebies",
                    callback_data="freebies_menu",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="👤 Profile",
                    callback_data="my_profile",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Referral Store",
                    callback_data="referrals_menu",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Support",
                    callback_data="support_ticket",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📦 My Orders",
                    callback_data="orders_menu",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Reseller API",
                    callback_data="api_key_menu",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Clear Menu",
                    callback_data="clear_start_menu",
                    style="danger",
                )
            ]
        ]
    )


def get_admin_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 SHOP",
                    callback_data="products_menu",
                    style="primary",
                )
            ],
            [InlineKeyboardButton(text="🛒 Cart", callback_data="ct_cart", style="primary"), InlineKeyboardButton(text="⚙ Preferences", callback_data="ct_preferences")],
            [
                InlineKeyboardButton(
                    text="💳 Wallet",
                    callback_data="deposit_start",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="🎁 Freebies",
                    callback_data="freebies_menu",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="👤 Profile",
                    callback_data="my_profile",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Referral Store",
                    callback_data="referrals_menu",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Support",
                    callback_data="support_ticket",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📦 My Orders",
                    callback_data="orders_menu",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Reseller API",
                    callback_data="api_key_menu",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Clear Menu",
                    callback_data="clear_start_menu",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👑 Admin",
                    callback_data="admin_panel",
                    style="danger",
                )
            ]
        ]
    )
