from sqlalchemy import text

from database import engine


def ensure_provider_schema() -> None:
    with engine.begin() as connection:
        exists = connection.execute(
            text(
                "SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'providers' "
                "AND COLUMN_NAME = 'api_secret'"
            )
        ).first()
        if not exists:
            try:
                connection.execute(text("ALTER TABLE providers ADD COLUMN api_secret TEXT NULL"))
            except Exception:
                exists = connection.execute(text(
                    "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='providers' AND COLUMN_NAME='api_secret'"
                )).first()
                if not exists:
                    raise
