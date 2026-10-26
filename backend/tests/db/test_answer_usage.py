"""
answer_usage and tenant_settings in the real database (ADR 0033, migration 0017).

What these tests pin down:
- Isolation: with no tenant set, nothing is visible; with tenant A set, only A's rows; B's rows and inserts for B are
  refused (RLS), and so is every write the grants do not give (UPDATE, DELETE, INSERT into settings).
- The metering table cannot hold text: the CHECK lists accept every code the application uses and refuse anything
  else, including a sentence in each text column; the lists match the code (compared with the live constraints).
- The cap query: the sum of today's cost for one tenant, as app_user.

The cluster admin writes the rows the tests need; the app connections never commit.
"""

import re
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from grounded_answers.answering import policy, usage

from .database import DbHandle
from .seed import Seeded

pytestmark = pytest.mark.db

SENTENCE = "What is my colleague's salary and why was I refused leave?"


def _app(handle: DbHandle) -> psycopg.Connection:
    return psycopg.connect(handle.url_for("app_user", handle.passwords["app_user"]))


def _set_tenant(conn: psycopg.Connection, tenant: uuid.UUID) -> None:
    conn.execute("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))


def _usage_values(tenant: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": tenant,
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "outcome": "answered",
        "attempts": 1,
        "retrieved_count": 5,
        "input_tokens": 1500,
        "output_tokens": 300,
        "cost_micro_usd": 400,
        "latency_ms": 900,
        "invalid_codes": [],
    }
    values.update(overrides)
    return values


INSERT_USAGE = (
    "INSERT INTO grounded.answer_usage (id, tenant_id, provider, model, outcome, attempts, "
    "retrieved_count, input_tokens, output_tokens, cost_micro_usd, latency_ms, invalid_codes) "
    "VALUES (%(id)s, %(tenant_id)s, %(provider)s, %(model)s, %(outcome)s, %(attempts)s, "
    "%(retrieved_count)s, %(input_tokens)s, %(output_tokens)s, %(cost_micro_usd)s, %(latency_ms)s, "
    "%(invalid_codes)s)"
)


@pytest.fixture
def settings_rows(grounded_db: DbHandle, seeded: Seeded) -> Seeded:
    """Both seeded tenants get a settings row (the seed inserts tenants directly, so the backfill missed them)."""
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        admin.execute(
            "INSERT INTO grounded.tenant_settings (tenant_id, answer_providers, data_class, "
            "daily_cost_cap_micro_usd) VALUES (%s, ARRAY['groq'], 'fictional', 50000), "
            "(%s, ARRAY['anthropic'], 'real', 1000000) "
            "ON CONFLICT (tenant_id) DO UPDATE SET answer_providers = EXCLUDED.answer_providers, "
            "data_class = EXCLUDED.data_class, "
            "daily_cost_cap_micro_usd = EXCLUDED.daily_cost_cap_micro_usd",
            (seeded.a.tenant, seeded.b.tenant),
        )
    return seeded


# ---- isolation ----


@pytest.mark.parametrize("table", ["tenant_settings", "answer_usage"])
def test_no_tenant_context_sees_nothing(
    grounded_db: DbHandle, settings_rows: Seeded, table: str
) -> None:
    with _app(grounded_db) as conn, conn.transaction():
        count = conn.execute(f"SELECT count(*) FROM grounded.{table}").fetchone()  # noqa: S608 - fixed names
    assert count == (0,)


def test_settings_are_visible_only_for_the_current_tenant(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    with _app(grounded_db) as conn, conn.transaction():
        _set_tenant(conn, settings_rows.a.tenant)
        rows = conn.execute(
            "SELECT tenant_id, answer_providers, data_class, daily_cost_cap_micro_usd "
            "FROM grounded.tenant_settings"
        ).fetchall()
    assert rows == [(settings_rows.a.tenant, ["groq"], "fictional", 50000)]


def test_usage_is_visible_only_for_the_current_tenant(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    a, b = settings_rows.a.tenant, settings_rows.b.tenant
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        admin.execute(INSERT_USAGE, _usage_values(b, cost_micro_usd=777))
        admin.execute(INSERT_USAGE, _usage_values(a, cost_micro_usd=111))
    with _app(grounded_db) as conn, conn.transaction():
        _set_tenant(conn, a)
        costs = {row[0] for row in conn.execute("SELECT cost_micro_usd FROM grounded.answer_usage")}
    assert 111 in costs
    assert 777 not in costs


def test_app_user_can_insert_its_own_tenants_usage(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    with _app(grounded_db) as conn:
        with conn.transaction():
            _set_tenant(conn, settings_rows.a.tenant)
            conn.execute(INSERT_USAGE, _usage_values(settings_rows.a.tenant))
        conn.rollback()


def test_app_user_cannot_insert_usage_for_another_tenant(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    with _app(grounded_db) as conn, pytest.raises(errors.InsufficientPrivilege) as excinfo:
        with conn.transaction():
            _set_tenant(conn, settings_rows.a.tenant)
            conn.execute(INSERT_USAGE, _usage_values(settings_rows.b.tenant))
    assert "row-level security" in str(excinfo.value)


def test_app_user_cannot_insert_usage_with_no_tenant_context(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    with _app(grounded_db) as conn, pytest.raises(errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute(INSERT_USAGE, _usage_values(settings_rows.a.tenant))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE grounded.answer_usage SET cost_micro_usd = 0",
        "DELETE FROM grounded.answer_usage",
        "INSERT INTO grounded.tenant_settings (tenant_id) VALUES (gen_random_uuid())",
        "UPDATE grounded.tenant_settings SET daily_cost_cap_micro_usd = 1000000000",
        "DELETE FROM grounded.tenant_settings",
    ],
)
def test_app_user_has_no_other_write_on_these_tables(
    grounded_db: DbHandle, settings_rows: Seeded, statement: str
) -> None:
    """No grant at all, so the refusal is 'permission denied' before any policy is looked at."""
    with _app(grounded_db) as conn, pytest.raises(errors.InsufficientPrivilege) as excinfo:
        with conn.transaction():
            _set_tenant(conn, settings_rows.a.tenant)
            conn.execute(statement)  # fixed test strings
    assert "permission denied" in str(excinfo.value)


def test_worker_user_has_no_access(grounded_db: DbHandle, settings_rows: Seeded) -> None:
    with (
        psycopg.connect(
            grounded_db.url_for("worker_user", grounded_db.passwords["worker_user"])
        ) as conn,
        pytest.raises(errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT count(*) FROM grounded.answer_usage")


# ---- the table cannot hold text ----


def test_every_code_the_application_uses_is_accepted(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        for outcome in usage.OUTCOMES:
            admin.execute(INSERT_USAGE, _usage_values(settings_rows.a.tenant, outcome=outcome))
        for code in usage.INVALID_CODES:
            admin.execute(
                INSERT_USAGE, _usage_values(settings_rows.a.tenant, invalid_codes=[code, code])
            )
        for provider in (*policy.KNOWN_PROVIDERS, None):
            admin.execute(INSERT_USAGE, _usage_values(settings_rows.a.tenant, provider=provider))
        admin.rollback()


@pytest.mark.parametrize(
    "override",
    [
        {"outcome": SENTENCE},
        {"outcome": "refused"},
        {"provider": SENTENCE},
        {"provider": "openai"},
        {"model": SENTENCE * 5},
        {"model": ""},
        {"invalid_codes": [SENTENCE]},
        {"invalid_codes": ["quote_not_found", "bad_ref", "bad_ref"]},
        {"attempts": 3},
        {"retrieved_count": 21},
        {"input_tokens": -1},
        {"output_tokens": 10_000_001},
        {"cost_micro_usd": -1},
        {"cost_micro_usd": 10_000_001},
        {"latency_ms": 600_001},
    ],
)
def test_a_value_outside_the_lists_is_refused_by_the_database(
    grounded_db: DbHandle, settings_rows: Seeded, override: dict[str, Any]
) -> None:
    with (
        psycopg.connect(grounded_db.admin_test_url()) as admin,
        pytest.raises(errors.CheckViolation),
    ):
        admin.execute(INSERT_USAGE, _usage_values(settings_rows.a.tenant, **override))


def test_the_only_free_text_columns_are_provider_model_outcome_and_codes(
    grounded_db: DbHandle,
) -> None:
    """A new text column on answer_usage fails here until a person decides it may exist (ADR 0033)."""
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        rows = admin.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'grounded' AND table_name = 'answer_usage' "
            "AND data_type IN ('text', 'character varying', 'character', 'ARRAY', 'jsonb', "
            "'json', 'bytea') ORDER BY 1"
        ).fetchall()
    assert [name for (name,) in rows] == ["invalid_codes", "model", "outcome", "provider"]


def _check_definition(admin: psycopg.Connection, name: str) -> str:
    row = admin.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = %s", (name,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_the_live_check_lists_match_the_code(grounded_db: DbHandle) -> None:
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        outcomes = re.findall(
            r"'([a-z_]+)'", _check_definition(admin, "answer_usage_outcome_check")
        )
        codes = re.findall(
            r"'([a-z_]+)'", _check_definition(admin, "answer_usage_invalid_codes_check")
        )
        providers = re.findall(
            r"'([a-z]+)'", _check_definition(admin, "answer_usage_provider_check")
        )
        settings_providers = re.findall(
            r"'([a-z]+)'", _check_definition(admin, "tenant_settings_providers_check")
        )
        classes = re.findall(
            r"'([a-z]+)'", _check_definition(admin, "tenant_settings_data_class_check")
        )
    assert tuple(outcomes) == usage.OUTCOMES
    assert tuple(codes) == usage.INVALID_CODES
    assert tuple(providers) == policy.KNOWN_PROVIDERS
    assert tuple(settings_providers) == policy.KNOWN_PROVIDERS
    assert tuple(classes) == policy.DATA_CLASSES


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("answer_providers", ["openai"]),
        ("answer_providers", ["groq", "gemini", "anthropic", "groq"]),
        ("data_class", "public"),
        ("daily_cost_cap_micro_usd", -1),
        ("daily_cost_cap_micro_usd", 1_000_000_001),
    ],
)
def test_settings_checks_refuse_bad_values(
    grounded_db: DbHandle, settings_rows: Seeded, column: str, value: Any
) -> None:
    statement = f"UPDATE grounded.tenant_settings SET {column} = %s WHERE tenant_id = %s"  # noqa: S608 - fixed test names
    with (
        psycopg.connect(grounded_db.admin_test_url()) as admin,
        pytest.raises(errors.CheckViolation),
    ):
        admin.execute(statement, (value, settings_rows.a.tenant))


# ---- the cap query ----


def test_the_daily_sum_counts_only_today_and_only_this_tenant(
    grounded_db: DbHandle, settings_rows: Seeded
) -> None:
    a, b = settings_rows.a.tenant, settings_rows.b.tenant
    with psycopg.connect(grounded_db.admin_test_url()) as admin:
        admin.execute("DELETE FROM grounded.answer_usage WHERE tenant_id IN (%s, %s)", (a, b))
        admin.execute(INSERT_USAGE, _usage_values(a, cost_micro_usd=100))
        admin.execute(INSERT_USAGE, _usage_values(a, cost_micro_usd=250))
        admin.execute(INSERT_USAGE, _usage_values(b, cost_micro_usd=9999))
        old = _usage_values(a, cost_micro_usd=5000)
        admin.execute(INSERT_USAGE, old)
        admin.execute(
            "UPDATE grounded.answer_usage SET created_at = now() - interval '2 days' WHERE id = %s",
            (old["id"],),
        )
    with _app(grounded_db) as conn, conn.transaction():
        _set_tenant(conn, a)
        total = conn.execute(
            "SELECT COALESCE(sum(cost_micro_usd), 0)::bigint FROM grounded.answer_usage "
            "WHERE tenant_id = %s AND created_at >= "
            "(date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc')",
            (a,),
        ).fetchone()
    assert total == (350,)
