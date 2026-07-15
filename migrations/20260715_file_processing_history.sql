CREATE TABLE IF NOT EXISTS file_processing_history (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    source_filename VARCHAR(255) NOT NULL,
    source_file_size BIGINT NOT NULL,
    category VARCHAR(20) NOT NULL,
    tool_type VARCHAR(64) NOT NULL,
    tool_label VARCHAR(120) NOT NULL,
    status VARCHAR(20) NOT NULL,
    output_filename VARCHAR(255) NULL,
    output_url VARCHAR(2048) NULL,
    output_expires_at BIGINT NULL,
    error_message VARCHAR(1000) NULL,
    created_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    KEY ix_file_processing_history_user_created (user_id, created_at),
    CONSTRAINT fk_file_processing_history_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
