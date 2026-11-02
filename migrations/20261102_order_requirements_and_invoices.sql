-- Run once against the RainDeals database if schema setup is not granted to the app.
ALTER TABLE products ADD COLUMN required_info JSON NULL;
ALTER TABLE orders ADD COLUMN reserved_delivery TEXT NULL;
ALTER TABLE deposits ADD COLUMN invoice_code VARCHAR(32) NULL;
ALTER TABLE deposits ADD COLUMN invoice_expires_at DATETIME NULL;
ALTER TABLE deposits ADD COLUMN invoice_usdt_amount DECIMAL(20,8) NULL;
CREATE UNIQUE INDEX uq_deposits_invoice_code ON deposits (invoice_code);

CREATE TABLE order_requirement_submissions (
    id INT NOT NULL AUTO_INCREMENT,
    order_id INT NOT NULL,
    telegram_id BIGINT NOT NULL,
    `values` JSON NOT NULL,
    submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_order_requirement_submission_order (order_id),
    KEY ix_order_requirement_submissions_telegram_id (telegram_id),
    CONSTRAINT fk_order_requirement_submissions_order
        FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
