-- Only run this migration when the previous users table was already created
-- with the display_code column.
ALTER TABLE users
    CHANGE COLUMN display_code internal_code CHAR(6) NOT NULL;

ALTER TABLE users
    DROP INDEX uq_users_display_code,
    ADD UNIQUE KEY uq_users_internal_code (internal_code);
