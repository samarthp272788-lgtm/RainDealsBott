from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, Boolean, func
from database import Base


class CheckoutAttempt(Base):
    __tablename__ = 'bot_checkout_attempts'
    action_key = Column(String(64), primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    product_id = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    order_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class OrderEvent(Base):
    __tablename__ = 'order_events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    actor_id = Column(BigInteger, nullable=True)
    summary = Column(String(300), nullable=False)
    customer_visible = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class OrderWorkNote(Base):
    __tablename__ = 'order_work_notes'
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False, index=True)
    admin_id = Column(BigInteger, nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
