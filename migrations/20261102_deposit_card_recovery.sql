-- Lets the bot update the same deposit card after a restart and mark unpaid
-- QR invoices expired without deleting their message.
ALTER TABLE deposits ADD COLUMN telegram_chat_id BIGINT NULL;
ALTER TABLE deposits ADD COLUMN telegram_message_id BIGINT NULL;
ALTER TABLE deposits ADD COLUMN verification_started_at DATETIME NULL;
