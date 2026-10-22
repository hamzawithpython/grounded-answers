"""
EXPECTED_SCHEMA_HEAD must equal the newest migration (ADR 0009, decision A).

Adding a migration without updating the constant fails here, so /ready can never compare
the database against a stale head.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from grounded_answers.readiness import EXPECTED_SCHEMA_HEAD

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_expected_schema_head_matches_newest_migration() -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head == EXPECTED_SCHEMA_HEAD
