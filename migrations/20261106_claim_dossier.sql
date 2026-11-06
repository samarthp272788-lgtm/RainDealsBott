-- Run only ALTERs not already applied by normal bot startup.
ALTER TABLE bot_warranty_requests ADD COLUMN issue_type VARCHAR(32) NOT NULL DEFAULT 'other';
ALTER TABLE bot_warranty_requests ADD COLUMN product_confirmed INT NOT NULL DEFAULT 0;
ALTER TABLE bot_warranty_requests ADD COLUMN invoice_snapshot JSON NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN delivery_snapshot TEXT NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN proof_image MEDIUMBLOB NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN proof_mime VARCHAR(32) NULL;
ALTER TABLE bot_warranty_requests ADD COLUMN bot_id BIGINT NULL;
