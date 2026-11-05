-- Optional manual setup if the app database user cannot alter schema.
-- Normal application startup checks/applies equivalent changes idempotently.
-- Run each ALTER only once, against the existing RainDeals database.
ALTER TABLE products ADD COLUMN warranty_days INT NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN warranty_days INT NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN warranty_expires_at DATETIME NULL;
ALTER TABLE orders ADD COLUMN admin_notification_queued_at DATETIME NULL;

CREATE TABLE IF NOT EXISTS bot_warranty_requests (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    telegram_id BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    evidence JSON NOT NULL,
    admin_id BIGINT NULL,
    resolution TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_bot_warranty_order (order_id),
    KEY ix_bot_warranty_telegram (telegram_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_notification_jobs (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_key VARCHAR(64) NOT NULL,
    bot_id BIGINT NOT NULL,
    chat_id VARCHAR(128) NOT NULL,
    body TEXT NOT NULL,
    reply_markup JSON NULL,
    status VARCHAR(20) NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at DATETIME NULL,
    sent_at DATETIME NULL,
    message_id BIGINT NULL,
    error_code VARCHAR(80) NULL,
    source_order_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_bot_notification_event (event_key),
    KEY ix_bot_notification_bot (bot_id),
    KEY ix_bot_notification_status (status),
    KEY ix_notification_destination_backoff (bot_id, chat_id, status, error_code, next_attempt_at),
    KEY ix_notification_delivery_queue (bot_id, status, next_attempt_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_provider_health (
    provider_key VARCHAR(100) NOT NULL PRIMARY KEY,
    requests INT NOT NULL DEFAULT 0,
    failures INT NOT NULL DEFAULT 0,
    consecutive_failures INT NOT NULL DEFAULT 0,
    last_success_at DATETIME NULL,
    last_failure_at DATETIME NULL,
    last_sync_at DATETIME NULL,
    last_error VARCHAR(100) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
