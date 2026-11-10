-- Run once only if the application DB user cannot add columns at startup.
ALTER TABLE deposits
    ADD COLUMN verification_lease_token VARCHAR(64) NULL,
    ADD COLUMN verification_lease_until DATETIME NULL;

CREATE INDEX ix_deposits_verification_lease_until
    ON deposits (verification_lease_until);
