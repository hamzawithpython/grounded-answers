"""Answer metering and per-tenant answer settings (ADR 0033, Session 5 Step 5a).

Revision ID: 0017_answer_usage_settings
Revises: 0016_drop_hnsw_index
Create Date: 2026-10-26

Two tables for POST /ask (built in Steps 5b and 5c).

tenant_settings: one row per tenant, written only by the operator (admin CLI, migration_role), read by the web app.
It says which answer providers the tenant may use, whether its data is fictional or real, and the daily cost cap in
millionths of a US dollar. A tenant with no row, or with no providers, gets no answers (fail closed). The migration
gives every existing tenant a row with the safe defaults: no providers, data_class 'real', cap 0.

answer_usage: one row per answered, refused or failed /ask request that reached the model stage, with tokens, cost,
latency and a coded outcome. It never holds the question, the sources, the answer or a quote, and it holds no user id
(ADR 0033, decision 4). That is enforced by the columns, not by the code: the only text columns are provider, model,
outcome and invalid_codes, and a CHECK limits provider, outcome and each invalid code to a fixed list, so free text
cannot be stored even by a bug. app_user may SELECT and INSERT its own tenant's rows (the daily cap sums them) and
nothing else: there is no UPDATE or DELETE grant for any role but the owner. The lists in the CHECKs mirror
grounded_answers.answering.policy (providers), grounded_answers.answering.respond and answering.validate (outcome and
invalid codes); tests compare them.

Rows are never removed in v1 (a retention or rollup job is v1.1 work).

This is a new migration; 0016 is applied (CLAUDE.md, migrations rule).
"""

from collections.abc import Sequence

from alembic import op

revision = "0017_answer_usage_settings"
down_revision = "0016_drop_hnsw_index"
branch_labels = None
depends_on = None

# The tenant predicate, as in 0003 and 0008: NULL when no tenant is set, so no row matches (fail closed).
CUR = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"

PROVIDERS = "ARRAY['groq', 'gemini', 'anthropic']::text[]"
OUTCOMES = (
    "answered",
    "refused_not_in_documents",
    "refused_needs_clarification",
    "refused_conflicting_sources",
    "could_not_verify",
    "declined",
    "no_sources",
    "timeout",
    "provider_error",
    "provider_rate_limited",
    "provider_disallowed",
    "tenant_not_configured",
    "service_error",
)
INVALID_CODES = (
    "invalid_output",
    "empty_answer",
    "no_citations",
    "bad_ref",
    "quote_ellipsis",
    "quote_too_short",
    "quote_too_long",
    "quote_not_informative",
    "quote_not_found",
    "number_not_grounded",
)


def _sql_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


UP: Sequence[str] = (
    f"""
    CREATE TABLE grounded.tenant_settings (
      tenant_id uuid PRIMARY KEY
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      answer_providers text[] NOT NULL DEFAULT '{{}}',
      data_class text NOT NULL DEFAULT 'real',
      daily_cost_cap_micro_usd bigint NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT tenant_settings_providers_check CHECK (
        answer_providers <@ {PROVIDERS} AND cardinality(answer_providers) <= 3),
      CONSTRAINT tenant_settings_data_class_check CHECK (data_class IN ('fictional', 'real')),
      CONSTRAINT tenant_settings_cap_check CHECK (
        daily_cost_cap_micro_usd BETWEEN 0 AND 1000000000)
    )
    """,
    f"""
    CREATE TABLE grounded.answer_usage (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL
        REFERENCES grounded.tenants (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
      created_at timestamptz NOT NULL DEFAULT now(),
      provider text NULL,
      model text NULL,
      outcome text NOT NULL,
      attempts smallint NOT NULL,
      retrieved_count smallint NOT NULL,
      input_tokens integer NOT NULL,
      output_tokens integer NOT NULL,
      cost_micro_usd bigint NOT NULL,
      latency_ms integer NOT NULL,
      invalid_codes text[] NOT NULL DEFAULT '{{}}',
      CONSTRAINT answer_usage_provider_check CHECK (
        provider IS NULL OR provider = ANY ({PROVIDERS})),
      CONSTRAINT answer_usage_model_check CHECK (
        model IS NULL OR char_length(model) BETWEEN 1 AND 200),
      CONSTRAINT answer_usage_outcome_check CHECK (outcome IN ({_sql_list(OUTCOMES)})),
      CONSTRAINT answer_usage_attempts_check CHECK (attempts BETWEEN 0 AND 2),
      CONSTRAINT answer_usage_retrieved_check CHECK (retrieved_count BETWEEN 0 AND 20),
      CONSTRAINT answer_usage_tokens_check CHECK (
        input_tokens BETWEEN 0 AND 10000000 AND output_tokens BETWEEN 0 AND 10000000),
      CONSTRAINT answer_usage_cost_check CHECK (cost_micro_usd BETWEEN 0 AND 10000000),
      CONSTRAINT answer_usage_latency_check CHECK (latency_ms BETWEEN 0 AND 600000),
      CONSTRAINT answer_usage_invalid_codes_check CHECK (
        invalid_codes <@ ARRAY[{_sql_list(INVALID_CODES)}]::text[]
        AND cardinality(invalid_codes) <= 2)
    )
    """,
    "CREATE INDEX answer_usage_tenant_created_idx ON grounded.answer_usage (tenant_id, created_at)",
    "ALTER TABLE grounded.tenant_settings ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.tenant_settings FORCE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.answer_usage ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE grounded.answer_usage FORCE ROW LEVEL SECURITY",
    # ---- app_user: read its own tenant's settings; read and add its own tenant's usage rows ----
    "CREATE POLICY app_user_tenant_settings_select ON grounded.tenant_settings "
    f"FOR SELECT TO app_user USING (tenant_id = {CUR})",
    "CREATE POLICY app_user_answer_usage_select ON grounded.answer_usage "
    f"FOR SELECT TO app_user USING (tenant_id = {CUR})",
    "CREATE POLICY app_user_answer_usage_insert ON grounded.answer_usage "
    f"FOR INSERT TO app_user WITH CHECK (tenant_id = {CUR})",
    # ---- migration_role (operator, ADR 0004): explicit, as on the other tenant tables ----
    "CREATE POLICY migration_role_tenant_settings_all ON grounded.tenant_settings "
    "FOR ALL TO migration_role USING (true) WITH CHECK (true)",
    "CREATE POLICY migration_role_answer_usage_all ON grounded.answer_usage "
    "FOR ALL TO migration_role USING (true) WITH CHECK (true)",
    # ---- grants: the least that the policies above need. Nobody but the owner can UPDATE or DELETE. ----
    "GRANT SELECT ON grounded.tenant_settings TO app_user",
    "GRANT SELECT, INSERT ON grounded.answer_usage TO app_user",
    # Every tenant that exists now gets the fail-closed defaults (no providers, 'real', cap 0).
    "INSERT INTO grounded.tenant_settings (tenant_id) SELECT id FROM grounded.tenants",
)

# Children before parents; neither table is referenced by another.
DOWN: Sequence[str] = (
    "DROP TABLE grounded.answer_usage",
    "DROP TABLE grounded.tenant_settings",
)


def upgrade() -> None:
    for statement in UP:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWN:
        op.execute(statement)
