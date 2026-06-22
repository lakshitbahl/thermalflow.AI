-- ThermalFlow — initial schema
-- Runs automatically on first boot via /docker-entrypoint-initdb.d (compose)
-- and via the postgres-init ConfigMap (k8s). Idempotent.

-- TimescaleDB extension. The timescale/timescaledb image preloads the shared
-- library but does NOT auto-create the extension in user databases — the
-- ingester's create_hypertable() depends on this existing.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS tenants (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  tenant_id INTEGER REFERENCES tenants(id),
  email VARCHAR(255) UNIQUE NOT NULL,
  roles TEXT[] DEFAULT ARRAY['viewer'],
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Audit log. Columns match exactly what the BFF inserts:
--   INSERT INTO audit_logs (user_id, tenant, action, details) VALUES (...)
-- The BFF authenticates from a self-contained JWT (claims: userId, tenant,
-- roles) and has no user/tenant row to reference yet, so user_id/tenant are
-- stored as the raw claim strings rather than FKs. Promote to FKs once a
-- real user-provisioning flow exists.
CREATE TABLE IF NOT EXISTS audit_logs (
  id SERIAL PRIMARY KEY,
  user_id VARCHAR(255),
  tenant  VARCHAR(100),
  action  VARCHAR(100),
  details JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_time
  ON audit_logs (tenant, created_at DESC);

-- thermal_zones hypertable is created at runtime by the ingester.
