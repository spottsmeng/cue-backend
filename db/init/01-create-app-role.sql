-- Runs once, automatically, on first Postgres container init (standard
-- postgres:17 image behaviour for files in /docker-entrypoint-initdb.d).
--
-- Why this exists: the bootstrap role (`cue`, from POSTGRES_USER) is a
-- superuser with rolbypassrls=true, per the official postgres image's
-- defaults. Row Level Security does not apply to superusers regardless of
-- FORCE ROW LEVEL SECURITY — so every RLS policy in this schema would be
-- silently inert if the application connected as `cue`. It must connect as
-- a genuinely unprivileged role instead. Migrations (DDL) still run as `cue`.
--
-- Password is pulled from the container's own environment (set via
-- docker-compose.yml, sourced from .env) rather than hardcoded here, since
-- this file is committed to the repo.
--
-- No IF NOT EXISTS guard: docker-entrypoint-initdb.d scripts run exactly
-- once, only against a freshly-initialized (empty) data directory.
\set app_password `echo "$CUE_APP_DB_PASSWORD"`

CREATE ROLE cue_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD :'app_password';

GRANT CONNECT ON DATABASE cue TO cue_app;
GRANT USAGE ON SCHEMA public TO cue_app;

-- Applies to tables Alembic creates later (migrations run after this script,
-- as `cue`, the schema owner) — no re-grant needed after each migration.
ALTER DEFAULT PRIVILEGES FOR ROLE cue IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cue_app;
ALTER DEFAULT PRIVILEGES FOR ROLE cue IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO cue_app;
