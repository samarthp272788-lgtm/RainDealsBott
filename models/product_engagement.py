"""Durable per-user product favorites and one-shot restock subscriptions."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ProductEngagement(Base):
    __tablename__ = "product_engagements"
    __table_args__ = (
        UniqueConstraint("telegram_id", "product_id", name="uq_product_engagement_user_product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    restock_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
