from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class OrderRequirementSubmission(Base):
    """Buyer supplied fields/files for a product order."""

    __tablename__ = "order_requirement_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    values: Mapped[dict] = mapped_column(JSON, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("order_id", name="uq_order_requirement_submission_order"),)
