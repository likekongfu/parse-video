CREATE TABLE IF NOT EXISTS documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    file_type VARCHAR(10) NOT NULL,
    file_size BIGINT NOT NULL,
    content_hash BINARY(32) NOT NULL,
    storage_path VARCHAR(1024) NOT NULL,
    extracted_text LONGTEXT NULL,
    extraction_status VARCHAR(20) NOT NULL,
    summary_json LONGTEXT NULL,
    summary_status VARCHAR(20) NOT NULL,
    error_message VARCHAR(1000) NULL,
    saved_at BIGINT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE KEY uq_documents_user_content_hash (user_id, content_hash),
    KEY ix_documents_user_created (user_id, created_at),
    CONSTRAINT fk_documents_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS document_tasks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    document_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    task_type VARCHAR(32) NOT NULL,
    status VARCHAR(20) NOT NULL,
    error_message VARCHAR(1000) NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    completed_at BIGINT NULL,
    UNIQUE KEY uq_document_tasks_document_type (document_id, task_type),
    KEY ix_document_tasks_user_created (user_id, created_at),
    CONSTRAINT fk_document_tasks_document FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    CONSTRAINT fk_document_tasks_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
