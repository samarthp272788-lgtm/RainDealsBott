"""Persistent customer aftercare, provider observations and notification outbox."""
from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, JSON, Index, Numeric, func
from database import Base


class WarrantyRequest(Base):
    __tablename__ = 'bot_warranty_requests'
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False, unique=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    status = Column(String(20), nullable=False, default='submitted')
    reason = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False)
    request_type = Column(String(16), nullable=False, default='warranty', server_default='warranty')
    refund_amount = Column(Numeric(20, 8), nullable=True)
    used_days = Column(Integer, nullable=True)
    remaining_days = Column(Integer, nullable=True)
    deactivated_at = Column(String(200), nullable=True)
    admin_id = Column(BigInteger, nullable=True)
    resolution = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class NotificationJob(Base):
    __tablename__ = 'bot_notification_jobs'
    __table_args__ = (
        Index('ix_notification_destination_backoff', 'bot_id', 'chat_id', 'status', 'error_code', 'next_attempt_at'),
        Index('ix_notification_delivery_queue', 'bot_id', 'status', 'next_attempt_at', 'id'),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(64), nullable=False, unique=True)
    bot_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(String(128), nullable=False)
    body = Column(Text, nullable=False)
    reply_markup = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default='queued', index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, server_default=func.now())
    claimed_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    message_id = Column(BigInteger, nullable=True)
    error_code = Column(String(80), nullable=True)
    source_order_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class ProviderHealth(Base):
    __tablename__ = 'bot_provider_health'
    provider_key = Column(String(100), primary_key=True)
    requests = Column(Integer, nullable=False, default=0)
    failures = Column(Integer, nullable=False, default=0)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_success_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    last_sync_at = Column(DateTime, nullable=True)
    last_error = Column(String(100), nullable=True)
