-- Database bootstrap (ADR 0006 amendment, layer b). Run ONCE per database, as the cluster admin.
-- Only the database owner or a superuser can grant CONNECT, which is why this runs as admin
-- and not as migration_role. Idempotent: safe to re-run.
--
-- ASCII ONLY: this file is piped to psql from PowerShell and tests. Non-ASCII bytes can be
-- mangled by PowerShell's default encoding. Keep every character in this file in ASCII.

\set ON_ERROR_STOP on

-- 1. Nobody connects by default. Only the three login roles get CONNECT.
DO $$
BEGIN
  EXECUTE format('REVOKE CONNECT, TEMP ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO migration_role, app_user, worker_user',
                 current_database());
END
$$;

-- 2. The public schema: no ordinary role creates objects there.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- 3. Our schema, owned by migration_role. Migrations create every object inside it.
CREATE SCHEMA IF NOT EXISTS grounded AUTHORIZATION migration_role;
DO $$
BEGIN
  IF (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'grounded')
     <> 'migration_role' THEN
    RAISE EXCEPTION 'schema grounded exists but is not owned by migration_role';
  END IF;
END
$$;

-- 3b. The job queue schema (ADR 0016), owned by migration_role. Created here, not in a
--     migration, because migration_role has no CREATE privilege on the database.
CREATE SCHEMA IF NOT EXISTS queue AUTHORIZATION migration_role;
DO $$
BEGIN
  IF (SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'queue')
     <> 'migration_role' THEN
    RAISE EXCEPTION 'schema queue exists but is not owned by migration_role';
  END IF;
END
$$;

-- 3c. The pgvector extension (ADR 0021), in its own schema. Not public: PUBLIC is revoked above and
--     the login roles have no privilege there. Not grounded: pgvector's functions and operators
--     would clutter the schema the catalog guard inspects. Owned by the admin who runs this file.
--     Only the three login roles get USAGE. Nobody gets CREATE. Every statement is idempotent.
CREATE SCHEMA IF NOT EXISTS extensions;
REVOKE ALL ON SCHEMA extensions FROM PUBLIC;
GRANT USAGE ON SCHEMA extensions TO migration_role, app_user, worker_user;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions;

-- 4. Per-database search_path. IN DATABASE keeps the setting scoped to this database,
--    because the grounded schema only exists in this one. app_user and worker_user also
--    get queue, because Procrastinate's SQL uses unqualified names (ADR 0016). Every role
--    gets extensions, so the vector type and its operators resolve. grounded stays first,
--    so tenant tables resolve exactly as before.
DO $$
BEGIN
  EXECUTE format('ALTER ROLE migration_role IN DATABASE %I SET search_path = grounded, extensions',
                 current_database());
  EXECUTE format('ALTER ROLE app_user IN DATABASE %I SET search_path = grounded, queue, extensions',
                 current_database());
  EXECUTE format('ALTER ROLE worker_user IN DATABASE %I SET search_path = grounded, queue, extensions',
                 current_database());
END
$$;
