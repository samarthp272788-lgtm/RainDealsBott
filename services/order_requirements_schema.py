"""Idempotent schema setup for buyer order information and shared invoices."""

from sqlalchemy import text

from database import Base, engine
from models.order_requirement_submission import OrderRequirementSubmission
from models.product_engagement import ProductEngagement
from models.admin_audit_log import AdminAuditLog
from models.bot_resume_session import BotResumeSession
from models.order_workflow import CheckoutAttempt, OrderEvent, OrderWorkNote
from models.bot_operations import WarrantyRequest, NotificationJob, ProviderHealth
from models.api_key import ApiKey, ApiOrder


def _ensure_column(connection, table: str, column: str, definition: str) -> bool:
    exists = connection.execute(text(
        "SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND COLUMN_NAME=:column"
    ), {"table": table, "column": column}).first()
    if not exists:
        connection.execute(text(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"))
        return True
    return False


def _ensure_unique_columns(connection, table, columns, name):
    found = connection.execute(text(
        'SELECT INDEX_NAME FROM information_schema.STATISTICS '
        'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND NON_UNIQUE=0 '
        'GROUP BY INDEX_NAME HAVING COUNT(*)=:count '
        'AND GROUP_CONCAT(COLUMN_NAME ORDER BY COLUMN_NAME)=:columns LIMIT 1'
    ), {'table': table, 'count': len(columns), 'columns': ','.join(sorted(columns))}).first()
    if not found:
        identifiers = ','.join(f'`{column}`' for column in columns)
        connection.execute(text(f'CREATE UNIQUE INDEX `{name}` ON `{table}` ({identifiers})'))


def _ensure_index(connection, table, columns, name):
    exists = connection.execute(text('SELECT 1 FROM information_schema.STATISTICS '
        'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table AND INDEX_NAME=:name LIMIT 1'),
        {'table': table, 'name': name}).first()
    if not exists:
        identifiers = ','.join(f'`{column}`' for column in columns)
        connection.execute(text(f'CREATE INDEX `{name}` ON `{table}` ({identifiers})'))


def ensure_order_requirements_schema() -> None:
    from models.customer_tools import CartItem, CartCheckout, CustomerPreferences, CustomerJobCursor, ClaimMessage, CancellationRequest, WalletEntry
    Base.metadata.create_all(
        bind=engine,
        tables=[
            OrderRequirementSubmission.__table__,
            ProductEngagement.__table__,
            AdminAuditLog.__table__,
            BotResumeSession.__table__,
            CheckoutAttempt.__table__, OrderEvent.__table__, OrderWorkNote.__table__,
            WarrantyRequest.__table__, NotificationJob.__table__, ProviderHealth.__table__,
            ApiKey.__table__, ApiOrder.__table__,
            CartItem.__table__, CartCheckout.__table__, CustomerPreferences.__table__, CustomerJobCursor.__table__,
            ClaimMessage.__table__, CancellationRequest.__table__, WalletEntry.__table__,
        ],
    )
    with engine.begin() as connection:
        if _ensure_column(connection,'bot_notification_jobs','inbox_read_at','DATETIME NULL'):
            connection.execute(text("UPDATE bot_notification_jobs SET inbox_read_at=UTC_TIMESTAMP() "
                                    "WHERE status IN ('sent','uncertain','resolved','failed')"))
        _ensure_index(connection,'bot_notification_jobs',['bot_id','chat_id','inbox_read_at','id'],'ix_notification_inbox')
        _ensure_column(connection, 'bot_notification_jobs', 'category', 'VARCHAR(24) NULL')
        _ensure_column(connection, 'orders', 'received_at', 'DATETIME NULL')
        _ensure_column(connection, 'orders', 'subscription_expires_at', 'DATETIME NULL')
        _ensure_column(connection, 'orders', 'term_days', 'INT NOT NULL DEFAULT 0')
        _ensure_column(connection, 'products', 'term_days', 'INT NOT NULL DEFAULT 0')
        _ensure_column(connection, "bot_notification_jobs", "reply_markup", "JSON NULL")
        _ensure_column(connection, "bot_warranty_requests", "request_type", "VARCHAR(16) NOT NULL DEFAULT 'warranty'")
        _ensure_column(connection, "bot_warranty_requests", "refund_amount", "DECIMAL(20,8) NULL")
        _ensure_column(connection, "bot_warranty_requests", "used_days", "INT NULL")
        _ensure_column(connection, "bot_warranty_requests", "remaining_days", "INT NULL")
        _ensure_column(connection, "bot_warranty_requests", "deactivated_at", "VARCHAR(200) NULL")
        for column, definition in {
            'issue_type': "VARCHAR(32) NOT NULL DEFAULT 'other'",
            'product_confirmed': 'INT NOT NULL DEFAULT 0', 'invoice_snapshot': 'JSON NULL',
            'delivery_snapshot': 'TEXT NULL', 'proof_image': 'MEDIUMBLOB NULL',
            'proof_mime': 'VARCHAR(32) NULL', 'bot_id': 'BIGINT NULL',
        }.items():
            _ensure_column(connection, 'bot_warranty_requests', column, definition)
        for table in ('products', 'orders'):
            _ensure_column(connection, table, 'aftercare_policy', 'VARCHAR(16) NULL')
            _ensure_column(connection, table, 'subscription_days', 'INT NOT NULL DEFAULT 0')
        _ensure_column(connection, 'orders', 'coverage_started_at', 'DATETIME NULL')
        _ensure_column(connection, 'orders', 'refund_amount', 'DECIMAL(20,8) NULL')
        _ensure_index(connection, 'bot_notification_jobs', ['bot_id','chat_id','status','error_code','next_attempt_at'], 'ix_notification_destination_backoff')
        _ensure_index(connection, 'bot_notification_jobs', ['bot_id','status','next_attempt_at','id'], 'ix_notification_delivery_queue')
        _ensure_unique_columns(connection, 'deposits', ['tx_hash'], 'uq_deposits_tx_hash')
        _ensure_unique_columns(connection, 'api_orders', ['api_key_id', 'client_order_id'], 'uq_api_order_client_id')
        _ensure_column(connection, "users", "home_menu_chat_id", "BIGINT NULL")
        _ensure_column(connection, "users", "home_menu_message_id", "BIGINT NULL")
        _ensure_column(connection, "products", "required_info", "JSON NULL")
        _ensure_column(connection, "products", "reusable_delivery", "TEXT NULL")
        _ensure_column(connection, "products", "reusable_stock", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "products", "warranty_days", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "warranty_days", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "warranty_expires_at", "DATETIME NULL")
        _ensure_column(connection, "orders", "admin_notification_queued_at", "DATETIME NULL")
        fallback_exists = connection.execute(text(
            "SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='products' AND COLUMN_NAME='manual_fallback'"
        )).first()
        _ensure_column(connection, "products", "manual_fallback", "TINYINT(1) NOT NULL DEFAULT 0")
        if not fallback_exists:
            connection.execute(text("UPDATE products SET manual_fallback=1 WHERE delivery_type IN ('manual','hybrid')"))
        # Normalize legacy own products that advertise automatic/hybrid
        # delivery without separate or reusable account inventory. Keep the
        # configured stock as manual sellable capacity and avoid prompting an
        # admin during startup. This runs after manual_fallback is guaranteed
        # to exist on older databases.
        connection.execute(text(
            "UPDATE products SET delivery_type='manual', manual_fallback=1, preorder=0 "
            "WHERE COALESCE(source,'own')='own' "
            "AND LOWER(COALESCE(delivery_type,'automatic')) IN ('automatic','hybrid') "
            "AND TRIM(COALESCE(file_content,''))='' "
            "AND (TRIM(COALESCE(reusable_delivery,''))='' OR COALESCE(reusable_stock,0)<=0)"
        ))
        _ensure_column(connection, "orders", "reserved_delivery", "TEXT NULL")
        _ensure_column(connection, "orders", "reusable_delivery_account", "TEXT NULL")
        _ensure_column(connection, "orders", "reusable_delivery_quantity", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "automatic_quantity", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "manual_pending_quantity", "INT NOT NULL DEFAULT 0")
        _ensure_column(connection, "orders", "stock_deducted_quantity", "INT NULL")
        _ensure_column(connection, "orders", "checkout_key", "VARCHAR(64) NULL")
        _ensure_column(connection, "orders", "assigned_admin_id", "BIGINT NULL")
        _ensure_column(connection, "orders", "assigned_at", "DATETIME NULL")
        _ensure_column(connection, "orders", "fulfillment_priority", "VARCHAR(12) NOT NULL DEFAULT 'normal'")
        checkout_index = connection.execute(text(
            "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='orders' AND COLUMN_NAME='checkout_key' AND NON_UNIQUE=0"
        )).first()
        if not checkout_index:
            connection.execute(text('CREATE UNIQUE INDEX uq_orders_checkout_key ON orders (checkout_key)'))
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
