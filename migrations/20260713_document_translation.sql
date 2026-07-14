CREATE TABLE IF NOT EXISTS document_translations (
    id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL PRIMARY KEY,
    document_id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL,
    user_id VARCHAR(36) COLLATE utf8mb4_unicode_ci NOT NULL,
    options_hash BINARY(32) NOT NULL,
    source_language VARCHAR(32) NOT NULL,
    detected_source_language VARCHAR(32) NULL,
    target_language VARCHAR(32) NOT NULL,
    mode VARCHAR(20) NOT NULL,
    style VARCHAR(20) NOT NULL,
    glossary_json LONGTEXT NULL,
    result_json LONGTEXT NULL,
    status VARCHAR(20) NOT NULL,
    error_message VARCHAR(1000) NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    UNIQUE KEY uq_document_translations_document_options (
        document_id, user_id, options_hash
    ),
    KEY ix_document_translations_user_created (user_id, created_at),
    CONSTRAINT fk_document_translations_document
        FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    CONSTRAINT fk_document_translations_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
