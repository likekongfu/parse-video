ALTER TABLE documents
    ADD COLUMN document_type VARCHAR(32) NULL AFTER extraction_status,
    ADD COLUMN document_type_source VARCHAR(20) NULL AFTER document_type;
