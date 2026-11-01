from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class ManagerLoginChallenge(Base):
    __tablename__ = "manager_login_challenges"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_manager_challenge_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    card_cleaned_at: Mapped[datetime | None] = mapped_column(DateTime)


class ManagerAuthSession(Base):
    __tablename__ = "manager_auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ManagerAuthState(Base):
    """Singleton pointer to the only session allowed to access the manager."""
    __tablename__ = "manager_auth_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class ManagerLoginAttempt(Base):
    __tablename__ = "manager_login_attempts"

    bucket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
