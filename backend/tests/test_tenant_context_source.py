"""Only the tenant helper may set the tenant or user context (ADR 0005, decision D1)."""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "grounded_answers"
HELPER = SOURCE / "tenant_db.py"
SETTING = re.compile(r"app\.current_(tenant|user)|set_config\s*\(")


def test_no_module_other_than_the_helper_sets_the_tenant_context() -> None:
    offenders = [
        path.name
        for path in sorted(SOURCE.rglob("*.py"))
        if path != HELPER and SETTING.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_helper_sets_both_values_with_set_config() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "set_config('app.current_tenant', :tenant_id, true)" in text
    assert "set_config('app.current_user', :user_id, true)" in text


def test_the_worker_helper_sets_the_tenant_only() -> None:
    """A job has no user (ADR 0017): its helper sets the tenant and nothing else."""
    text = HELPER.read_text(encoding="utf-8")
    worker_sql = text.split("SET_WORKER_CONTEXT_SQL = text(", 1)[1].split("\n", 1)[0]
    assert "set_config('app.current_tenant', :tenant_id, true)" in worker_sql
    assert "app.current_user" not in worker_sql
