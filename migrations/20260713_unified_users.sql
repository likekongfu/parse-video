CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    internal_code CHAR(6) NOT NULL,
    display_name VARCHAR(120) NULL,
    avatar_url VARCHAR(1024) NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE KEY uq_users_internal_code (internal_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_identities (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    provider_user_id VARCHAR(191) NOT NULL,
    unionid VARCHAR(191) NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE KEY uq_user_identity_provider_user (provider, provider_user_id),
    KEY ix_user_identities_unionid (unionid),
    KEY ix_user_identities_user_id (user_id),
    CONSTRAINT fk_user_identities_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS qr_login_sessions (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    scene_token_hash BINARY(32) NOT NULL,
    login_ticket_hash BINARY(32) NULL,
    user_id VARCHAR(36) NULL,
    status VARCHAR(20) NOT NULL,
    expires_at BIGINT NOT NULL,
    confirmed_at BIGINT NULL,
    consumed_at BIGINT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE KEY uq_qr_login_scene_token_hash (scene_token_hash),
    UNIQUE KEY uq_qr_login_ticket_hash (login_ticket_hash),
    KEY ix_qr_login_sessions_status_expires (status, expires_at),
    CONSTRAINT fk_qr_login_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
