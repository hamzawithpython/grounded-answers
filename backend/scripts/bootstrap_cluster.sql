-- Cluster bootstrap (ADR 0006 amendment, layer a). Run ONCE per cluster, as the cluster admin.
-- Idempotent: safe to re-run. Passwords arrive as SCRAM-SHA-256 verifiers in environment
-- variables, computed by scripts/make_scram_verifier.py (libpq), never as plaintext.
--
-- ASCII ONLY: this file is piped to psql from PowerShell and tests. Non-ASCII bytes can be
-- mangled by PowerShell's default encoding. Keep every character in this file in ASCII.

\set ON_ERROR_STOP on

-- 1. Create missing roles. CREATE ROLE has no IF NOT EXISTS, so existence is checked first.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      ('migration_role', true),
      ('app_user', true),
      ('worker_user', true),
      ('auth_definer', false),
      ('retention_definer', false)
    ) AS t(name, can_login)
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r.name) THEN
      IF r.can_login THEN
        EXECUTE format('CREATE ROLE %I LOGIN', r.name);
      ELSE
        EXECUTE format('CREATE ROLE %I NOLOGIN', r.name);
      END IF;
    END IF;
  END LOOP;
END
$$;

-- 2. Role attributes, re-asserted on every run so drift from an earlier run is corrected.
--    NOINHERIT: a login role never picks up privileges through role memberships.
ALTER ROLE migration_role NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE app_user NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE worker_user NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE auth_definer NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;
ALTER ROLE retention_definer NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT;

-- 3. Verifiers from the environment. An unset variable leaves the psql variable undefined,
--    so each check stops the run with a clear message (ON_ERROR_STOP).
\getenv migration_role_scram MIGRATION_ROLE_SCRAM
\getenv app_user_scram APP_USER_SCRAM
\getenv worker_user_scram WORKER_USER_SCRAM
\if :{?migration_role_scram}
\else
DO $$ BEGIN RAISE EXCEPTION 'MIGRATION_ROLE_SCRAM is not set'; END $$;
\endif
\if :{?app_user_scram}
\else
DO $$ BEGIN RAISE EXCEPTION 'APP_USER_SCRAM is not set'; END $$;
\endif
\if :{?worker_user_scram}
\else
DO $$ BEGIN RAISE EXCEPTION 'WORKER_USER_SCRAM is not set'; END $$;
\endif

-- 4. Passwords. PostgreSQL stores a SCRAM string as-is (a verifier, not plaintext).
ALTER ROLE migration_role PASSWORD :'migration_role_scram';
ALTER ROLE app_user PASSWORD :'app_user_scram';
ALTER ROLE worker_user PASSWORD :'worker_user_scram';

-- 5. The migration role may SET ROLE to the definer roles (needed for ALTER FUNCTION ... OWNER TO)
--    but does not inherit their privileges. GRANT takes one WITH option per statement.
GRANT auth_definer TO migration_role WITH INHERIT FALSE;
GRANT auth_definer TO migration_role WITH SET TRUE;
GRANT retention_definer TO migration_role WITH INHERIT FALSE;
GRANT retention_definer TO migration_role WITH SET TRUE;
