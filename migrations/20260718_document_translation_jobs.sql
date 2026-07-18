CREATE TABLE IF NOT EXISTS document_translation_jobs (
    id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL PRIMARY KEY,
    document_id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL,
    user_id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL,
    options_hash BINARY(32) NOT NULL,
    options_json LONGTEXT NOT NULL,
    translation_id VARCHAR(36) COLLATE utf8mb4_unicode_ci NULL,
    request_id VARCHAR(36) NOT NULL,
    status VARCHAR(20) NOT NULL,
    completed_batches BIGINT NOT NULL DEFAULT 0,
    total_batches BIGINT NOT NULL DEFAULT 0,
    cached BIGINT NOT NULL DEFAULT 0,
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(1000) NULL,
    created_at BIGINT NOT NULL,
    started_at BIGINT NULL,
    updated_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    expires_at BIGINT NOT NULL,
    UNIQUE KEY uq_document_translation_jobs_options (
        document_id, user_id, options_hash
    ),
    KEY ix_document_translation_jobs_user_updated (user_id, updated_at),
    KEY ix_document_translation_jobs_status_expires (status, expires_at),
    CONSTRAINT fk_document_translation_jobs_document
        FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    CONSTRAINT fk_document_translation_jobs_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
