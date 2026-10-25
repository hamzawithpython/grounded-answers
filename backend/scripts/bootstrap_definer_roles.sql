-- Definer roles (ADR 0007, 0023, 0024; SECURITY.md, "Definer functions"). Run as the cluster admin.
--
-- Needs NO secrets: a definer role is NOLOGIN and has no password, so this file never reads a verifier.
-- Idempotent: safe to re-run. To add a definer role, add its name to the list below and run this one command:
--   Get-Content -Raw backend\scripts\bootstrap_definer_roles.sql | docker compose exec -T postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 -f -
--
-- Run bootstrap_cluster.sql (the login roles) first. It creates migration_role, which this file grants the
-- definer roles to. If migration_role is missing, this file stops with a clear error and changes nothing.
--
-- ASCII ONLY: this file is piped to psql from PowerShell and tests. Keep every character in this file in ASCII.

\set ON_ERROR_STOP on

DO $$
DECLARE
  r text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migration_role') THEN
    RAISE EXCEPTION 'migration_role does not exist: run bootstrap_cluster.sql first';
  END IF;

  FOREACH r IN ARRAY ARRAY[
      'auth_definer',
      'retention_definer',
      'provisioning_definer',
      'retry_definer',
      'swap_definer',
      'purge_definer'
    ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
    -- NOINHERIT and no superuser, bypass or login: a definer role holds no rights of its own, only the
    -- rights of the functions it owns (SECURITY.md, "Definer functions").
    EXECUTE format(
      'ALTER ROLE %I NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT', r);
    -- migration_role may SET ROLE to a definer to change a function's owner (ALTER FUNCTION ... OWNER TO),
    -- but never inherits the definer's rights. GRANT takes one WITH option per statement.
    EXECUTE format('GRANT %I TO migration_role WITH INHERIT FALSE', r);
    EXECUTE format('GRANT %I TO migration_role WITH SET TRUE', r);
  END LOOP;
END
$$;
