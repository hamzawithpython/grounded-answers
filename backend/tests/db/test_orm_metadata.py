"""
The ORM models and the migrated schema must not drift apart (ADR 0005, decision B1).

1. Alembic's autogenerate comparison, run against the migrated test database, must find no
   difference in tables, columns, types, nullability, keys, unique constraints or indexes.
   Policies, grants, functions and triggers are not compared: they are explicit SQL (ADR 0005).
2. CHECK constraints are not in autogenerate, so their names and definitions are compared with
   tests/db/expected_checks.json. A changed rule under the same name fails the test.
"""

import json
import warnings
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.exc import SAWarning

from grounded_answers.models import Base
from tests.db.database import DbHandle

pytestmark = pytest.mark.db

EXPECTED_CHECKS = Path(__file__).with_name("expected_checks.json")


def _include_name(name: str | None, type_: str, parent_names: Any) -> bool:
    """Compare the grounded schema only. The migration bookkeeping table is not a model."""
    if type_ == "schema":
        return name == "grounded"
    if type_ == "table":
        return name != "alembic_version"
    return True


def test_models_match_the_migrated_schema(grounded_db: DbHandle) -> None:
    url = grounded_db.admin_test_url().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "include_name": _include_name,
                    "compare_type": True,
                },
            )
            # documents and document_versions reference each other, so Alembic cannot order
            # them. That warning only affects output order. The mutation check in the
            # review of this change showed FKs in the cycle are still compared.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", message="Cannot correctly sort tables", category=SAWarning
                )
                differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert differences == [], "ORM models and migrations differ:\n" + "\n".join(
        repr(d) for d in differences
    )


def test_check_constraints_match_the_reviewed_list(grounded_db: DbHandle) -> None:
    """Names and definitions, as PostgreSQL stores them. A changed rule fails this test."""
    with psycopg.connect(grounded_db.admin_test_url()) as conn:
        rows = conn.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE connamespace = 'grounded'::regnamespace AND contype = 'c' "
            "ORDER BY 1, 2"
        ).fetchall()
    actual = [
        {"table": table.removeprefix("grounded."), "name": name, "definition": definition}
        for table, name, definition in rows
    ]
    expected = json.loads(EXPECTED_CHECKS.read_text(encoding="utf-8"))
    assert actual == expected
