-- Upgrade once; skip any ALTER already applied by Python startup schema setup.
-- NULL policy retains existing warranty_days behavior. No existing order policy is rewritten.
ALTER TABLE products ADD COLUMN aftercare_policy VARCHAR(16) NULL;
ALTER TABLE products ADD COLUMN subscription_days INT NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN aftercare_policy VARCHAR(16) NULL;
ALTER TABLE orders ADD COLUMN subscription_days INT NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN coverage_started_at DATETIME NULL;
ALTER TABLE orders ADD COLUMN refund_amount DECIMAL(20,8) NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN request_type VARCHAR(16) NOT NULL DEFAULT 'warranty';
ALTER TABLE bot_warranty_requests ADD COLUMN refund_amount DECIMAL(20,8) NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN used_days INT NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN remaining_days INT NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN deactivated_at VARCHAR(200) NULL;
