from sqlalchemy import BigInteger, Column, DateTime, JSON, PrimaryKeyConstraint
from database import Base


class BotResumeSession(Base):
    __tablename__ = 'bot_resume_sessions'
    telegram_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    snapshot = Column(JSON, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    __table_args__ = (PrimaryKeyConstraint('telegram_id', 'chat_id'),)
