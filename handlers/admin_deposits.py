from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from decimal import Decimal, ROUND_HALF_UP

from database import SessionLocal
from config import ADMIN_IDS
from models.deposit import Deposit

router = Router()
ADMIN_DEPOSITS_PER_PAGE = 10


def is_admin(user_id: int):
    return user_id in ADMIN_IDS


def _format_value(value, places: str) -> str:
    try:
        rounded = Decimal(str(value or 0)).quantize(Decimal(places), rounding=ROUND_HALF_UP)
        return format(rounded, "f").rstrip("0").rstrip(".") or "0"
    except Exception:
        return "0"


def _admin_deposit_amount(deposit) -> str:
    if (deposit.network or "").upper() == "UPI" and deposit.status == "completed":
        credited = _format_value(deposit.received_amount or deposit.amount, "0.001")
        if deposit.inr_amount is not None:
            return f"₹{_format_value(deposit.inr_amount, '0.01')} → ${credited}"
        return f"${credited}"
    if (deposit.network or "").upper() == "UPI":
        return f"₹{_format_value(deposit.amount, '0.01')}"
    return f"${_format_value(deposit.received_amount or deposit.amount, '0.001')}"


# ==================================================
# DEPOSIT LIST
# ==================================================

@router.callback_query(
    F.data == "admin_deposits"
)
@router.callback_query(F.data.startswith("admin_deposits_page_"))
@router.callback_query(F.data.startswith("admin_deposits_filter_"))
async def admin_deposits(
        callback: CallbackQuery
):

    if not is_admin(
            callback.from_user.id
    ):

        await callback.answer(
            "Access denied.",
            show_alert=True
        )
        return

    allowed_statuses = {"all", "pending", "waiting_hash", "completed", "failed", "expired"}
    status = "all"
    page = 0
    parts = callback.data.split("_")
    try:
        if len(parts) >= 4 and parts[2] == "filter":
            status = parts[3] if parts[3] in allowed_statuses else "all"
        elif len(parts) >= 4 and parts[2] == "page":
            # Legacy links used admin_deposits_page_N.
            if parts[3].isdigit():
                page = max(0, int(parts[3]))
            elif len(parts) >= 5:
                status = parts[3] if parts[3] in allowed_statuses else "all"
                page = max(0, int(parts[4]))
    except (TypeError, ValueError):
        status, page = "all", 0

    db = SessionLocal()

    try:

        query = db.query(Deposit)
        if status == "pending":
            query = query.filter(Deposit.status.in_(["pending", "waiting_hash"]))
        elif status != "all":
            query = query.filter(Deposit.status == status)
        query = query.order_by(Deposit.id.desc())
        total = query.count()
        total_pages = max(1, (total + ADMIN_DEPOSITS_PER_PAGE - 1) // ADMIN_DEPOSITS_PER_PAGE)
        page = min(page, total_pages - 1)
        deposits = query.offset(page * ADMIN_DEPOSITS_PER_PAGE).limit(ADMIN_DEPOSITS_PER_PAGE).all()

        if not deposits:

            await callback.message.edit_text(
                f"❌ No deposits found for <b>{status.replace('_', ' ').title()}</b>.",
                parse_mode="HTML",
                reply_markup=
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="All", callback_data="admin_deposits_filter_all"),
                            InlineKeyboardButton(text="⏳ Pending", callback_data="admin_deposits_filter_pending"),
                            InlineKeyboardButton(text="✅ Paid", callback_data="admin_deposits_filter_completed"),
                        ],
                        [
                            InlineKeyboardButton(
                                text="⬅ Back",
                                callback_data="admin_panel"
                            )
                        ]
                    ]
                )
            )

            await callback.answer()
            return

        keyboard = []

        for deposit in deposits:

            if deposit.status == "completed":
                icon = "✅"

            elif deposit.status == "failed":
                icon = "❌"

            else:
                icon = "⏳"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"#{deposit.id} "
                            f"{icon} "
                            f"{_admin_deposit_amount(deposit)}"
                        ),

                        callback_data=f"deposit_{deposit.id}_{page}_{status}"
                    )
                ]
            )

        keyboard.append([
            InlineKeyboardButton(text="All", callback_data="admin_deposits_filter_all"),
            InlineKeyboardButton(text="⏳ Pending", callback_data="admin_deposits_filter_pending"),
            InlineKeyboardButton(text="✅ Paid", callback_data="admin_deposits_filter_completed"),
            InlineKeyboardButton(text="❌ Failed", callback_data="admin_deposits_filter_failed"),
        ])
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="⬅ Previous", callback_data=f"admin_deposits_page_{status}_{page - 1}"))
        if page < total_pages - 1:
            navigation.append(InlineKeyboardButton(text="Next ➡", callback_data=f"admin_deposits_page_{status}_{page + 1}"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data="admin_panel"
                )
            ]
        )

        await callback.message.edit_text(
            f"💰 Deposits · {status.replace('_', ' ').title()} ({page + 1}/{total_pages}) — {total} total",
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=keyboard
            )
        )

        await callback.answer()

    finally:
        db.close()


# ==================================================
# SINGLE DEPOSIT
# ==================================================

@router.callback_query(
    F.data.regexp(r"^deposit_\d+_")
)
async def deposit_info(
        callback: CallbackQuery
):

    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied.", show_alert=True)
        return

    parts = callback.data.split("_")
    deposit_id = int(parts[1])
    list_page = int(parts[2]) if len(parts) > 2 else 0
    list_status = parts[3] if len(parts) > 3 and parts[3] in {"all", "pending", "waiting_hash", "completed", "failed", "expired"} else "all"

    db = SessionLocal()

    try:

        deposit = (
            db.query(Deposit)
            .filter(
                Deposit.id == deposit_id
            )
            .first()
        )

        if not deposit:

            await callback.answer(
                "Deposit not found."
            )
            return

        text = f"""
💰 Deposit #{deposit.id}

👤 User ID:
<code>{deposit.telegram_id}</code>

💵 Amount:
{_admin_deposit_amount(deposit)}

🌐 Network:
{deposit.network}

🔗 TXID:
<code>{deposit.tx_hash or "Not Submitted"}</code>

📄 Status:
{deposit.status}

📅 Date:
{deposit.created_at}
"""

        keyboard = [

            [
                InlineKeyboardButton(
                    text="⬅ Back",
                    callback_data=(
                        "admin_deposits_filter_all" if list_page == 0 and list_status == "all"
                        else f"admin_deposits_filter_{list_status}" if list_page == 0
                        else f"admin_deposits_page_{list_status}_{list_page}"
                    )
                )
            ]
        ]

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=
            InlineKeyboardMarkup(
                inline_keyboard=keyboard
            )
        )

        await callback.answer()

    finally:
        db.close()
