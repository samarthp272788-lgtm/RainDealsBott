"""Recover and expire Telegram deposit cards using saved deposit state."""

import asyncio
import logging
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import SessionLocal
from models.deposit import Deposit
from utils.ui import schedule_delete_card

logger = logging.getLogger(__name__)


def _recover_interrupted_deposits() -> list[dict]:
    """Put submitted references back in the checker queue after a restart."""
    db = SessionLocal()
    try:
        deposits = (
            db.query(Deposit)
            .filter(Deposit.status == "waiting_hash", Deposit.tx_hash.isnot(None))
            .order_by(Deposit.id.asc())
            .limit(500)
            .all()
        )
        recovered = []
        for deposit in deposits:
            deposit.status = "pending"
            deposit.verification_started_at = None
            recovered.append({
                "id": deposit.id,
                "chat_id": deposit.telegram_chat_id or deposit.telegram_id,
                "message_id": deposit.telegram_message_id,
                "network": deposit.network or "",
                "tx_hash": deposit.tx_hash or "",
            })
        if recovered:
            db.commit()
        return recovered
    except Exception:
        db.rollback()
        logger.exception("Could not recover interrupted deposit verifications")
        return []
    finally:
        db.close()


def _expire_invoice_cards() -> list[dict]:
    """Expire unpaid invoices in the database and return their Telegram cards."""
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        deposits = (
            db.query(Deposit)
            .filter(
                Deposit.status == "waiting_hash",
                Deposit.tx_hash.is_(None),
                Deposit.invoice_expires_at.isnot(None),
                Deposit.invoice_expires_at <= now,
            )
            .order_by(Deposit.id.asc())
            .limit(100)
            .all()
        )
        expired = []
        for deposit in deposits:
            deposit.status = "expired"
            expired.append({
                "id": deposit.id,
                "chat_id": deposit.telegram_chat_id or deposit.telegram_id,
                "message_id": deposit.telegram_message_id,
                "network": deposit.network or "",
                "code": deposit.invoice_code or deposit.order_id or "",
            })
        if expired:
            db.commit()
        return expired
    except Exception:
        db.rollback()
        logger.exception("Could not expire old deposit invoices")
        return []
    finally:
        db.close()


def _recovery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 My Deposits", callback_data="my_deposits")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
    ])


def _expired_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 New Deposit", callback_data="deposit_start")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")],
    ])


async def _edit_card(bot, item: dict, text: str, markup: InlineKeyboardMarkup):
    chat_id = item.get("chat_id")
    message_id = item.get("message_id")
    if not chat_id or not message_id:
        return None
    try:
        return await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("Could not reach Telegram to update deposit card %s", item.get("id"))
        return None
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.info("Could not update deposit card %s (%s)", item.get("id"), exc)
    except Exception:
        logger.exception("Could not update deposit card %s", item.get("id"))
    return None


async def recover_interrupted_deposit_cards(bot) -> None:
    """Resume submitted deposits and replace stale spinner cards at startup."""
    recovered = await asyncio.to_thread(_recover_interrupted_deposits)
    for item in recovered:
        network = str(item.get("network") or "").upper()
        network_label = {"BEP20": "🟡 BEP20 (BSC)", "POLYGON": "🟣 Polygon", "BINANCE": "🔴 Binance Pay", "UPI": "🟢 UPI"}.get(network, network)
        reference = str(item.get("tx_hash") or "")
        if len(reference) > 28:
            reference = reference[:24] + "..."
        text = (
            "⏳ <b>DEPOSIT VERIFICATION RESUMED</b>\n\n"
            "The bot restarted while checking this payment. Your submitted reference is saved, "
            "and automatic verification will continue.\n\n"
            f"🆔 <b>Deposit:</b> <code>#{item['id']}</code>\n"
            f"📡 <b>Network:</b> {network_label}\n"
            f"🔑 <b>Reference:</b> <code>{reference}</code>\n\n"
            "You do not need to submit it again."
        )
        await _edit_card(bot, item, text, _recovery_keyboard())
    if recovered:
        logger.info("Recovered %d interrupted deposit verification(s)", len(recovered))


async def deposit_invoice_expiry_loop(bot, interval_seconds: int = 30) -> None:
    """Show expiry status, then clear stale unpaid QR cards after 15 minutes."""
    while True:
        expired = await asyncio.to_thread(_expire_invoice_cards)
        for item in expired:
            text = (
                "⏰ <b>DEPOSIT INVOICE EXPIRED</b>\n\n"
                f"🆔 <b>Deposit:</b> <code>#{item['id']}</code>\n"
                f"🧾 <b>Invoice:</b> <code>{item['code']}</code>\n\n"
                "This invoice expired before a payment reference was submitted. "
                "Create a new deposit to get a fresh invoice."
            )
            expired_card = await _edit_card(bot, item, text, _expired_keyboard())
            if expired_card:
                schedule_delete_card(
                    expired_card,
                    owner_id=int(item.get("chat_id") or 0),
                    delay_seconds=900,
                )
        await asyncio.sleep(max(5, interval_seconds))
