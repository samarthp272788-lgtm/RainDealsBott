-- Apply to telegram_shop if the application DB user cannot create tables at startup.
-- Safe for existing data; this adds audit history and a single-poller lease.

CREATE TABLE IF NOT EXISTS bot_warranty_request_revisions (
    id INT NOT NULL AUTO_INCREMENT,
    request_id INT NOT NULL,
    order_id INT NOT NULL,
    revision INT NOT NULL,
    telegram_id BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    evidence JSON NOT NULL,
    request_type VARCHAR(16) NOT NULL,
    refund_amount DECIMAL(20,8) NULL,
    used_days INT NULL,
    remaining_days INT NULL,
    deactivated_at VARCHAR(200) NULL,
    issue_type VARCHAR(32) NULL,
    product_confirmed INT NOT NULL DEFAULT 0,
    invoice_snapshot JSON NULL,
    delivery_snapshot TEXT NULL,
    proof_image MEDIUMBLOB NULL,
    proof_mime VARCHAR(32) NULL,
    admin_id BIGINT NULL,
    resolution TEXT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_bot_warranty_request_revisions_request_id (request_id),
    KEY ix_bot_warranty_request_revisions_order_id (order_id),
    KEY ix_warranty_revision_order (order_id, archived_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS bot_polling_leases (
    bot_id BIGINT NOT NULL,
    owner_token VARCHAR(64) NOT NULL,
    lease_until DATETIME NOT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bot_id),
    KEY ix_bot_polling_leases_lease_until (lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
