-- Shared reseller API rate-limit buckets. The application also creates this
-- table idempotently during startup; this SQL is for restricted DB users.
CREATE TABLE IF NOT EXISTS api_rate_limit_buckets (
    bucket_hash CHAR(64) NOT NULL,
    request_count INT NOT NULL DEFAULT 0,
    expires_at DATETIME NOT NULL,
    PRIMARY KEY (bucket_hash),
    KEY ix_api_rate_limit_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
