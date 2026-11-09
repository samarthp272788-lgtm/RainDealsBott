# models/product.py

"""
Product model — SQLAlchemy 2.x declarative style.

Supports:
- Own products
- Reseller/provider-linked products
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.provider import Provider


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    icon: Mapped[Optional[str]] = mapped_column(
        String(50),
        default="📦",
    )

    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[Optional[str]] = mapped_column(
        String(255),
        default="General",
        nullable=True,
    )

    # Optional delivery instructions shown to buyer AFTER purchase.
    delivery_instruction: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        default=None,
    )

    # Buyer information requested after checkout, e.g. email, phone, photo.
    # List entries: {"type": "email|phone|photo|contact|text", "label": "..."}.
    required_info: Mapped[Optional[list]] = mapped_column(JSON, nullable=True, default=None)
    warranty_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')
    # NULL preserves the earlier warranty_days policy; new setup explicitly chooses.
    aftercare_policy: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    term_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    subscription_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default='0')

    # DECIMAL(20, 8) — always compare/multiply as Decimal.
    price: Mapped[Decimal] = mapped_column(
        Numeric(20, 8),
        nullable=False,
        default=Decimal("0.00000000"),
    )

    # Manual/local stock counter.
    # For reseller products this is NOT the source of truth.
    stock: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Newline-separated pool of accounts/keys for own products.
    file_content: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # One reusable credential can represent many sellable units. This keeps
    # stock such as 1,000 units from requiring 1,000 copies in file_content.
    reusable_delivery: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reusable_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    # Controls only public reseller-API visibility and ordering.  It does not
    # affect the normal Telegram shop, which continues to use is_active.
    api_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
    )

    # "automatic", "manual", or "hybrid"
    delivery_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="automatic",
        server_default="automatic",
    )

    manual_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )

    # If True, customers can still order at 0 stock.
    preorder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # Admin alert threshold.
    low_stock_threshold: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3,
        server_default="3",
    )

    # JSON storage for tiered/bulk pricing rules.
    bulk_pricing: Mapped[Optional[dict]] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )

    # ============================================================
    # MULTI-PROVIDER & RESELLER FIELDS
    # ============================================================

    # Product source: "own" or "reseller"
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="own",
        server_default="own",
    )

    # Direct Foreign Key linking to providers table
    provider_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # Reseller service/product ID on external API
    reseller_service_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        default=None,
    )

    # Original price charged by the reseller
    reseller_cost: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 8),
        nullable=True,
        default=None,
    )

    # Name/identifier of the reseller configuration for backward compatibility
    reseller_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        default=None,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # ORM Relationship (string reference avoids duplicate table declarations)
    provider: Mapped[Optional["Provider"]] = relationship(
        "Provider", back_populates="products"
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_id", "reseller_service_id", name="uq_provider_reseller_service"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Product "
            f"id={self.id} "
            f"name={self.name!r} "
            f"source={self.source!r} "
            f"provider_id={self.provider_id} "
            f"stock={self.stock}>"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize product model to dictionary safely."""
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "description": self.description,
            "category": self.category,
            "delivery_instruction": self.delivery_instruction,
            "aftercare_policy": self.aftercare_policy,
            "warranty_days": self.warranty_days,
            "subscription_days": self.subscription_days,
            "price": str(self.price) if self.price is not None else "0.00000000",
            "stock": self.stock,
            "reusable_stock": self.reusable_stock,
            "is_active": self.is_active,
            "api_enabled": self.api_enabled,
            "delivery_type": self.delivery_type,
            "manual_fallback": self.manual_fallback,
            "preorder": self.preorder,
            "low_stock_threshold": self.low_stock_threshold,
            "bulk_pricing": self.bulk_pricing,
            "source": self.source,
            "provider_id": self.provider_id,
            "reseller_service_id": self.reseller_service_id,
            "reseller_cost": str(self.reseller_cost) if self.reseller_cost is not None else None,
            "reseller_name": self.reseller_name,
            "provider": self.provider.to_dict() if self.provider and hasattr(self.provider, "to_dict") else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
