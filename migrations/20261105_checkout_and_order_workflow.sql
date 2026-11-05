-- Optional manual setup if the bot database user cannot alter schema.
-- The normal application startup performs equivalent, idempotent checks.
-- Run the ALTER statements once on the existing RainDeals database.
ALTER TABLE orders ADD COLUMN checkout_key VARCHAR(64) NULL;
ALTER TABLE orders ADD COLUMN assigned_admin_id BIGINT NULL;
ALTER TABLE orders ADD COLUMN assigned_at DATETIME NULL;
ALTER TABLE orders ADD COLUMN fulfillment_priority VARCHAR(12) NOT NULL DEFAULT 'normal';
CREATE UNIQUE INDEX uq_orders_checkout_key ON orders (checkout_key);

CREATE TABLE IF NOT EXISTS bot_checkout_attempts (
    action_key VARCHAR(64) NOT NULL PRIMARY KEY,
    telegram_id BIGINT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,
    status VARCHAR(20) NOT NULL,
    order_id INT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_bot_checkout_attempts_telegram_id (telegram_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_events (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    kind VARCHAR(40) NOT NULL,
    actor_id BIGINT NULL,
    summary VARCHAR(300) NOT NULL,
    customer_visible TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_order_events_order_id (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS order_work_notes (
    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    admin_id BIGINT NOT NULL,
    body TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY ix_order_work_notes_order_id (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
