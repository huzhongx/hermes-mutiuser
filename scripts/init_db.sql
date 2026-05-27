-- ============================================================
-- Hermes 多租户平台数据库初始化
-- ============================================================

-- 租户表
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        VARCHAR(128) NOT NULL UNIQUE,
    api_key     VARCHAR(64)  NOT NULL UNIQUE,
    plan        VARCHAR(32)  NOT NULL DEFAULT 'basic',
    max_sessions INTEGER NOT NULL DEFAULT 10,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

-- 会话表
CREATE TABLE IF NOT EXISTS sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    status          VARCHAR(32) NOT NULL DEFAULT 'initializing',
    hermes_home     TEXT NOT NULL,
    hermes_port     INTEGER NOT NULL,
    pid             INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    error_message   TEXT,
    metadata        JSONB DEFAULT '{}'::JSONB
);

-- 消息表（审计日志）
CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tenant_id   UUID NOT NULL,
    role        VARCHAR(16) NOT NULL,
    content     TEXT NOT NULL,
    tokens      INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- API 调用日志表
CREATE TABLE IF NOT EXISTS api_logs (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID,
    session_id  UUID,
    endpoint    VARCHAR(256) NOT NULL,
    method      VARCHAR(16)  NOT NULL,
    status_code INTEGER,
    latency_ms  INTEGER,
    ip_address  INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_id    ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status       ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_last_active  ON sessions(last_active_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_id   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_api_logs_tenant_id    ON api_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_logs_created_at   ON api_logs(created_at);

-- 预置测试租户
INSERT INTO tenants (name, api_key, plan, max_sessions)
VALUES
    ('tenant_alpha', 'sk-alp-haaa0001', 'pro', 20),
    ('tenant_beta',  'sk-bet-hbbb0002', 'basic', 5)
ON CONFLICT (name) DO NOTHING;
