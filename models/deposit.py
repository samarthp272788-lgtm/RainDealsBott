from sqlalchemy import (
    BigInteger,
    Column,
    Integer,
    String,
    DateTime,
    DECIMAL
)
from sqlalchemy.sql import func

from database import Base


class Deposit(Base):
    __tablename__ = "deposits"

    id = Column(
        Integer,
        primary_key=True,
        autoincrement=True
    )

    telegram_id = Column(Integer)

    order_id = Column(
        String(100),
        unique=True
    )

    invoice_code = Column(String(32), unique=True, nullable=True, index=True)
    invoice_expires_at = Column(DateTime, nullable=True, index=True)
    invoice_usdt_amount = Column(DECIMAL(20, 8), nullable=True)
    telegram_chat_id = Column(BigInteger, nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    verification_started_at = Column(DateTime, nullable=True, index=True)
    verification_lease_token = Column(String(64), nullable=True)
    verification_lease_until = Column(DateTime, nullable=True, index=True)

    tx_hash = Column(
        String(255),
        unique=True,
        nullable=True
    )

    amount = Column(
        DECIMAL(20, 8)
    )

    # `amount` is the final USDT amount after a completed deposit. For UPI,
    # retain the original INR payment and the exact conversion used so users
    # and admins can verify what was paid and what was credited.
    received_amount = Column(
        DECIMAL(20, 8),
        nullable=True,
    )

    inr_amount = Column(
        DECIMAL(20, 2),
        nullable=True,
    )

    conversion_rate = Column(
        DECIMAL(20, 8),
        nullable=True,
    )

    network = Column(
        String(50)
    )

    status = Column(
        String(50),
        default="waiting_hash"
    )

    created_at = Column(
        DateTime,
        server_default=func.now()
    )
