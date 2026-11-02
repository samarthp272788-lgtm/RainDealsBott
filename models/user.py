"""
models/user.py

User model — SQLAlchemy 2.x declarative style.

Money fields (balance, total_spent, total_deposited,
referral_earnings) are DECIMAL(20, 8), matching Order.amount and
Product.price. Do not do float math against these attributes anywhere
in the codebase — always Decimal(str(x)) if you need to combine them
with a literal, and only call float() at the very last moment, when
formatting text for Telegram.

is_banned is a real Boolean now instead of an Integer 0/1 flag. A
MySQL/TiDB BOOLEAN column is just an alias for TINYINT(1), so this is
a compatible in-place type change for existing 0/1 data — see
migrations/migrate_user_decimal.py.
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    # ------------------------------------------------------------
    # Telegram Information
    # ------------------------------------------------------------

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        nullable=False,
        index=True,
    )

    username: Mapped[Optional[str]] = mapped_column(String(100))

    # Keep one durable home-menu card per Telegram user so /start and
    # Main Menu can edit it even after a bot restart.
    home_menu_chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    home_menu_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # ------------------------------------------------------------
    # Wallet — all DECIMAL(20, 8), never Float
    # ------------------------------------------------------------

    balance: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # ------------------------------------------------------------
    # Referral System
    # ------------------------------------------------------------

    referral_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        unique=True,
    )

    referred_by: Mapped[Optional[int]] = mapped_column(BigInteger)

    total_referrals: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    referral_earnings: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # ------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------

    total_orders: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    total_spent: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    total_deposited: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )

    # ------------------------------------------------------------
    # User Status
    # ------------------------------------------------------------

    is_banned: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # ------------------------------------------------------------
    # Display-only helpers — float conversion happens HERE and only
    # here, right before formatting text for Telegram. Never use these
    # in a comparison or another calculation; use the Decimal
    # attribute itself for that.
    # ------------------------------------------------------------

    @property
    def balance_display(self) -> float:
        return float(self.balance)

    @property
    def total_spent_display(self) -> float:
        return float(self.total_spent)

    @property
    def total_deposited_display(self) -> float:
        return float(self.total_deposited)

    @property
    def referral_earnings_display(self) -> float:
        return float(self.referral_earnings)

    def __repr__(self) -> str:
        return f"<User id={self.id} telegram_id={self.telegram_id}>"
