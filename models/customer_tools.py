"""Durable customer tools; every record is scoped to its Telegram owner."""
from sqlalchemy import Column, Integer, BigInteger, String, Text, JSON, DateTime, Numeric, Boolean, UniqueConstraint, LargeBinary, func
from sqlalchemy.orm import deferred
from sqlalchemy.dialects.mysql import MEDIUMBLOB
from database import Base


class CartItem(Base):
    __tablename__ = 'bot_cart_items'
    __table_args__ = (UniqueConstraint('telegram_id', 'product_id', name='uq_cart_owner_product'),)
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    product_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)
    revision = Column(String(32), nullable=False)


class CartCheckout(Base):
    __tablename__ = 'bot_cart_checkouts'
    token = Column(String(32), primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    status = Column(String(20), nullable=False, default='ready')
    lines = Column(JSON, nullable=False)
    results = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())


class CustomerPreferences(Base):
    __tablename__ = 'bot_customer_preferences'
    telegram_id = Column(BigInteger, primary_key=True)
    restock = Column(Boolean, nullable=False, default=True)
    order_progress = Column(Boolean, nullable=False, default=True)
    subscription = Column(Boolean, nullable=False, default=True)


class CustomerJobCursor(Base):
    __tablename__ = 'bot_customer_job_cursors'
    name = Column(String(50), primary_key=True)
    last_order_id = Column(Integer, nullable=False, default=0)


class ClaimMessage(Base):
    __tablename__ = 'bot_claim_messages'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, nullable=False, index=True)
    actor_id = Column(BigInteger, nullable=False)
    role = Column(String(12), nullable=False)
    text = Column(Text, nullable=False)
    image = deferred(Column(LargeBinary().with_variant(MEDIUMBLOB(), 'mysql'), nullable=True))
    mime = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())


class CancellationRequest(Base):
    __tablename__ = 'bot_cancellation_requests'
    order_id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    bot_id = Column(BigInteger, nullable=False)
    status = Column(String(20), nullable=False, default='submitted')
    admin_id = Column(BigInteger, nullable=True)
    resolution = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())


class WalletEntry(Base):
    __tablename__ = 'bot_wallet_entries'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    event_key = Column(String(100), nullable=False, unique=True)
    label = Column(String(200), nullable=False)
    amount = Column(Numeric(20, 8), nullable=False)
    balance = Column(Numeric(20, 8), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now())
