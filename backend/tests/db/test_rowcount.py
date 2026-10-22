"""expect_rowcount on real writes under RLS (SECURITY R9)."""

import logging
from typing import Any

import pytest
from sqlalchemy import text

from grounded_answers.database import create_session_factory
from grounded_answers.rowcount import RowcountMismatchError, expect_rowcount
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle
from tests.db.seed import Seeded
from tests.db.test_orm_models import app_engine, membership_active, membership_id

pytestmark = pytest.mark.db

DEACTIVATE_SQL = text("UPDATE grounded.tenant_memberships SET active = false WHERE id = :id")


@pytest.mark.anyio
async def test_zero_rows_from_an_rls_filtered_update_raises_and_logs_no_data(
    grounded_db: DbHandle, seeded: Seeded, caplog: pytest.LogCaptureFixture
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    other_membership = membership_id(grounded_db, seeded.b.admin)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        with caplog.at_level(logging.ERROR, logger="grounded_answers.rowcount"):
            async with begin_tenant_transaction(factory, context) as session:
                result: Any = await session.execute(DEACTIVATE_SQL, {"id": other_membership})
                with pytest.raises(RowcountMismatchError, match="affected 0 rows, expected 1"):
                    expect_rowcount(result, 1, "membership.deactivate")
    finally:
        await engine.dispose()

    assert "membership.deactivate affected 0 rows, expected 1" in caplog.text
    assert str(other_membership) not in caplog.text, "the log line must not carry row ids"
    assert str(seeded.b.admin) not in caplog.text
    assert membership_active(grounded_db, seeded.b.admin) is True


@pytest.mark.anyio
async def test_matching_rowcount_passes_and_logs_nothing(
    grounded_db: DbHandle, seeded: Seeded, caplog: pytest.LogCaptureFixture
) -> None:
    context = TenantContext(tenant_id=seeded.a.tenant, user_id=seeded.a.admin)
    own_membership = membership_id(grounded_db, seeded.a.employee)
    engine = await app_engine(grounded_db)
    try:
        factory = create_session_factory(engine)
        with caplog.at_level(logging.ERROR, logger="grounded_answers.rowcount"):
            async with begin_tenant_transaction(factory, context) as session:
                # Sets active to its current value: one row matches and nothing changes.
                result: Any = await session.execute(
                    text("UPDATE grounded.tenant_memberships SET active = active WHERE id = :id"),
                    {"id": own_membership},
                )
                expect_rowcount(result, 1, "membership.touch")
    finally:
        await engine.dispose()
    assert caplog.text == ""
