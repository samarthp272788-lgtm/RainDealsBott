"""
models/order.py

Order model — SQLAlchemy 2.x declarative style.

Stores both normal/local orders and reseller/API fulfilled orders.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        index=True,
    )

    product_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    product_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # DECIMAL(20, 8) — never Float.
    amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0"),
    )

    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    # "automatic" | "manual" | "hybrid"
    delivery_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="automatic",
        server_default="automatic",
    )

    is_preorder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # "completed" | "pending_manual" | "processing" | "preorder" | "refunded"
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="completed",
        server_default="completed",
    )

    refunded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # Locally delivered account/code.
    delivered_account: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Reserved stock withheld until the buyer submits required order details.
    reserved_delivery: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    automatic_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    manual_pending_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stock_deducted_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checkout_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    assigned_admin_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    fulfillment_priority: Mapped[str] = mapped_column(String(12), nullable=False, default='normal', server_default='normal')
    warranty_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    warranty_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    aftercare_policy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    subscription_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    coverage_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    refund_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), nullable=True)
    admin_notification_queued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # For a manual reseller API order, this is the reseller's end customer.
    # The separate DELIVERY_BOT_TOKEN bot receives the completed delivery.
    # NULL means deliver using the normal store-bot buyer ID.
    delivery_telegram_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        index=True,
    )

    # ============================================================
    # RESELLER ORDER INFORMATION
    # ============================================================

    # ID of the reseller configuration/provider used for this order.
    # Foreign key referencing providers.id
    #
    # Example:
    # reseller_id = 1
    #
    # NULL means this was a normal/local product order.
    reseller_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The reseller's product/service ID.
    #
    # Example:
    # service_1784565602
    #
    # This is the ID sent to POST /api/v1/order or provider purchase endpoint
    reseller_service_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    # Order ID returned by the reseller API after purchasing.
    #
    # Example:
    # API_ABC123XYZ
    #
    # NULL until the reseller purchase succeeds.
    reseller_order_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
    )

    # Relationship to Provider model
    provider: Mapped[Optional["Provider"]] = relationship(
        "Provider",
        lazy="selectin",
    )

    # ============================================================
    # TIMESTAMP
    # ============================================================

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )

    # Set only after the dedicated admin-order group receives this order.
    # This makes notifications durable across restarts and prevents duplicates.
    admin_notification_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<Order "
            f"id={self.id} "
            f"telegram_id={self.telegram_id} "
            f"product_id={self.product_id} "
            f"qty={self.quantity} "
            f"reseller_id={self.reseller_id} "
            f"reseller_service_id={self.reseller_service_id!r} "
            f"reseller_order_id={self.reseller_order_id!r} "
            f"status={self.status!r}>"
        )
