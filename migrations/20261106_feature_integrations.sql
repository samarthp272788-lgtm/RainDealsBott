-- Existing installations only. Normal bot startup checks these idempotently.
-- Check SHOW COLUMNS / SHOW INDEX first and skip columns/indexes already present.
-- Apply against the configured RainDeals database, never against sys.
ALTER TABLE bot_notification_jobs ADD COLUMN reply_markup JSON NULL;
CREATE INDEX ix_notification_destination_backoff
    ON bot_notification_jobs (bot_id,chat_id,status,error_code,next_attempt_at);
CREATE INDEX ix_notification_delivery_queue
    ON bot_notification_jobs (bot_id,status,next_attempt_at,id);

-- Equivalent unique indexes with other names already satisfy these requirements.
-- If legacy duplicates prevent index creation, review them before deployment;
-- do not delete payment history or reset wallet balances to force it through.
CREATE UNIQUE INDEX uq_deposits_tx_hash ON deposits (tx_hash);
CREATE UNIQUE INDEX uq_api_order_client_id ON api_orders (api_key_id,client_order_id);
