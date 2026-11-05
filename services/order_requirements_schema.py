"""Idempotent schema setup for buyer order information and shared invoices."""

from sqlalchemy import text

from database import Base, engine
from models.order_requirement_submission import OrderRequirementSubmission
from models.product_engagement import ProductEngagement
from models.admin_audit_log import AdminAuditLog
from models.bot_resume_session import BotResumeSession


def _ensure_column(connection, table: str, column: str, definition: str) -> None:
    exists = connection.execute(text(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND COLUMN_NAME=:column"
    ), {"table": table, "column": column}).first()
    if not exists:
        connection.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"))


def ensure_order_requirements_schema() -> None:
    Base.metadata.create_all(
        bind=engine,
        tables=[
            OrderRequirementSubmission.__table__,
            ProductEngagement.__table__,
            AdminAuditLog.__table__,
            BotResumeSession.__table__,
        ],
    )
    with engine.begin() as connection:
        _ensure_column(connection, "users", "home_menu_chat_id", "BIGINT NULL")
        _ensure_column(connection, "users", "home_menu_message_id", "BIGINT NULL")
        _ensure_column(connection, "products", "required_info", "JSON NULL")
        fallback_exists = connection.execute(text(
            "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='products' AND COLUMN_NAME='manual_fallback'"
        )).first()
        _ensure_column(connection, "products", "manual_fallback", "TINYINT(1) NOT NULL DEFAULT 0")
        if not fallback_exists:
            connection.execute(text("UPDATE products SET manual_fallback=1 WHERE delivery_type IN ('manual','hybrid')"))
        _ensure_column(connection, "orders", "reserved_delivery", "TEXT NULL")
        _ensure_column(connection, "orders", "automatic_quantity", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "manual_pending_quantity", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "stock_deducted_quantity", "INT NULL")
        _ensure_column(connection, "deposits", "invoice_code", "VARCHAR(32) NULL")
        _ensure_column(connection, "deposits", "invoice_expires_at", "DATETIME NULL")
        _ensure_column(connection, "deposits", "invoice_usdt_amount", "DECIMAL(20,8) NULL")
        _ensure_column(connection, "deposits", "telegram_chat_id", "BIGINT NULL")
        _ensure_column(connection, "deposits", "telegram_message_id", "BIGINT NULL")
        _ensure_column(connection, "deposits", "verification_started_at", "DATETIME NULL")
        index_exists = connection.execute(text(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='deposits' "
            "AND COLUMN_NAME='invoice_code' AND NON_UNIQUE=0"
        )).first()
        if not index_exists:
            connection.execute(text(
                "CREATE UNIQUE INDEX uq_deposits_invoice_code ON deposits (invoice_code)"
            ))
