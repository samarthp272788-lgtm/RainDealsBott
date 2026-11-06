-- Customer tools. The bot startup schema helper applies missing columns idempotently.


CREATE TABLE IF NOT EXISTS bot_cancellation_requests (
	order_id INTEGER NOT NULL AUTO_INCREMENT, 
	telegram_id BIGINT NOT NULL, 
	reason TEXT NOT NULL, 
	bot_id BIGINT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	admin_id BIGINT, 
	resolution TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (order_id)
)

;

CREATE INDEX ix_bot_cancellation_requests_telegram_id ON bot_cancellation_requests (telegram_id);


CREATE TABLE IF NOT EXISTS bot_cart_checkouts (
	token VARCHAR(32) NOT NULL, 
	telegram_id BIGINT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	`lines` JSON NOT NULL, 
	results JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (token)
)

;

CREATE INDEX ix_bot_cart_checkouts_telegram_id ON bot_cart_checkouts (telegram_id);


CREATE TABLE IF NOT EXISTS bot_cart_items (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	telegram_id BIGINT NOT NULL, 
	product_id INTEGER NOT NULL, 
	quantity INTEGER NOT NULL, 
	revision VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_cart_owner_product UNIQUE (telegram_id, product_id)
)

;

CREATE INDEX ix_bot_cart_items_telegram_id ON bot_cart_items (telegram_id);


CREATE TABLE IF NOT EXISTS bot_claim_messages (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	order_id INTEGER NOT NULL, 
	actor_id BIGINT NOT NULL, 
	`role` VARCHAR(12) NOT NULL, 
	text TEXT NOT NULL, 
	image MEDIUMBLOB, 
	mime VARCHAR(32), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_bot_claim_messages_order_id ON bot_claim_messages (order_id);


CREATE TABLE IF NOT EXISTS bot_customer_job_cursors (
	name VARCHAR(50) NOT NULL, 
	last_order_id INTEGER NOT NULL, 
	PRIMARY KEY (name)
)

;


CREATE TABLE IF NOT EXISTS bot_customer_preferences (
	telegram_id BIGINT NOT NULL AUTO_INCREMENT, 
	restock BOOL NOT NULL, 
	order_progress BOOL NOT NULL, 
	subscription BOOL NOT NULL, 
	PRIMARY KEY (telegram_id)
)

;


CREATE TABLE IF NOT EXISTS bot_wallet_entries (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	telegram_id BIGINT NOT NULL, 
	event_key VARCHAR(100) NOT NULL, 
	label VARCHAR(200) NOT NULL, 
	amount NUMERIC(20, 8) NOT NULL, 
	balance NUMERIC(20, 8) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (event_key)
)

;

CREATE INDEX ix_bot_wallet_entries_telegram_id ON bot_wallet_entries (telegram_id);

-- Apply the following columns only if absent; startup checks this automatically.

-- ALTER TABLE products ADD COLUMN term_days INT NOT NULL DEFAULT 0;

-- ALTER TABLE orders ADD COLUMN term_days INT NOT NULL DEFAULT 0;

-- ALTER TABLE orders ADD COLUMN subscription_expires_at DATETIME NULL;

-- ALTER TABLE orders ADD COLUMN received_at DATETIME NULL;

-- ALTER TABLE bot_notification_jobs ADD COLUMN category VARCHAR(24) NULL;
