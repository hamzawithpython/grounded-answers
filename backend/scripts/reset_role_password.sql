-- Reset the password of ONE login role (RUNBOOK, "Reset one role's password").
-- Idempotent. ASCII ONLY: this file is piped to psql from PowerShell.
--
-- Inputs:
--   psql variable  role        (-v role=app_user)   must be a login role below
--   environment    ROLE_SCRAM  (SCRAM verifier from scripts/make_scram_verifier.py)

\set ON_ERROR_STOP on

\getenv role_scram ROLE_SCRAM
\if :{?role_scram}
\else
DO $$ BEGIN RAISE EXCEPTION 'ROLE_SCRAM is not set'; END $$;
\endif
\if :{?role}
\else
DO $$ BEGIN RAISE EXCEPTION 'pass -v role=migration_role, app_user or worker_user'; END $$;
\endif

-- Allowlist: only the three login roles. Any other name (postgres, a definer role) stops here.
SELECT :'role' IN ('migration_role', 'app_user', 'worker_user') AS role_ok \gset
\if :role_ok
\else
DO $$ BEGIN RAISE EXCEPTION 'role must be migration_role, app_user or worker_user'; END $$;
\endif

ALTER ROLE :"role" PASSWORD :'role_scram';
