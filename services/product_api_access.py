"""Schema support for the per-product public API on/off switch."""

from sqlalchemy import text

from database import engine


def ensure_product_api_access_schema() -> None:
    """Add the non-destructive API toggle column on existing installations."""
    with engine.begin() as connection:
        exists = connection.execute(
            text(
                "SELECT 1 FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'products' "
                "AND COLUMN_NAME = 'api_enabled'"
            )
        ).first()
        if not exists:
            try:
                connection.execute(text(
                    "ALTER TABLE products ADD COLUMN api_enabled TINYINT(1) NOT NULL DEFAULT 1"
                ))
            except Exception:
                exists = connection.execute(text(
                    "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='products' AND COLUMN_NAME='api_enabled'"
                )).first()
                if not exists:
                    raise
