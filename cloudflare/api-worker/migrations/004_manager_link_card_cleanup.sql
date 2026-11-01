-- Save the originating RainDeals bot card so the Manager Worker can
-- remove a one-time sign-in button after use or expiration.
ALTER TABLE manager_login_challenges
    ADD COLUMN telegram_chat_id BIGINT NULL,
    ADD COLUMN telegram_message_id BIGINT NULL,
    ADD COLUMN card_cleaned_at DATETIME NULL;

CREATE INDEX ix_manager_login_card_cleanup
    ON manager_login_challenges (expires_at, consumed_at, card_cleaned_at);
