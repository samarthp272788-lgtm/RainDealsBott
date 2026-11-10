"""API credentials and idempotent API-order records for external resellers."""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # The complete key is encrypted at rest so its Telegram owner can view it
    # again. Authentication always uses key_hash, never this encrypted value.
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    encrypted_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ApiOrder(Base):
    """Maps an external idempotency ID to a single internal order."""

    __tablename__ = "api_orders"
    __table_args__ = (
        UniqueConstraint("api_key_id", "client_order_id", name="uq_api_order_client_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_order_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # Binds a retry key to the exact purchase request. A client cannot reuse
    # one idempotency key with different product, quantity, recipient, or data.
    request_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processing", server_default="processing")
    error_message: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ApiRateLimitBucket(Base):
    """Shared fixed-window API counters; only opaque hashes are stored."""

    __tablename__ = "api_rate_limit_buckets"

    bucket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
