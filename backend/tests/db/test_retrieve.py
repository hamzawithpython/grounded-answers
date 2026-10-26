"""
retrieve() against the real database as app_user (ADR 0030): hybrid search over the tenant's current chunks.

Vectors are built from weighted axes (chunk_support.py), so the vector order is known in advance and the model is not
needed. The keyword side is real PostgreSQL full-text search. Each test makes its own tenants and removes them.
"""

import hashlib
from collections.abc import Callable, Sequence

import psycopg
import pytest

from grounded_answers.retrieval import RetrievedChunk, retrieve
from grounded_answers.retrieval.rankers import vector_rank
from grounded_answers.tenant_db import TenantContext, begin_tenant_transaction
from tests.db.database import DbHandle

from .chunk_support import ChunkSpec, VersionSpec, as_floats, axis_vector, insert_document
from .eval_support import EvalTenant, app_sessions

pytestmark = [pytest.mark.db, pytest.mark.anyio]

QUERY = axis_vector({0: 1.0})


def chunk(body: str, weights: dict[int, float], **kwargs: object) -> ChunkSpec:
    return ChunkSpec(body=body, vector=axis_vector(weights), **kwargs)  # type: ignore[arg-type]


async def search(
    handle: DbHandle,
    tenant: EvalTenant,
    question: str,
    vector: str = QUERY,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    async with app_sessions(handle) as factory:
        async with begin_tenant_transaction(
            factory, TenantContext(tenant.tenant, tenant.employee)
        ) as session:
            return await retrieve(session, question, as_floats(vector), top_k=top_k)


def bodies(chunks: Sequence[RetrievedChunk]) -> list[str]:
    return [c.body for c in chunks]


async def test_a_result_carries_the_text_title_version_and_clause_labels(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    tenant = tenants()
    document = insert_document(
        admin,
        tenant,
        "Leave Policy",
        [
            VersionSpec(
                [
                    chunk(
                        "Probation is six months.",
                        {0: 1.0},
                        section_number="3",
                        heading="Probation",
                        clause_labels=("3", "3.2"),
                    )
                ]
            )
        ],
    )
    [result] = await search(grounded_db, tenant, "how long is probation")
    assert result.body == "Probation is six months."
    assert result.title == "Leave Policy"
    assert result.version_no == 1
    assert result.section_number == "3"
    assert result.heading == "Probation"
    assert result.clause_labels == ("3", "3.2")
    assert result.document_id == document.document_id
    assert result.version_id == document.version_ids[0]
    assert result.vector_rank == 1 and result.keyword_rank == 1
    assert result.score > 0


async def test_a_replaced_version_is_never_returned(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    tenant = tenants()
    document = insert_document(
        admin,
        tenant,
        "Expense Policy",
        [
            VersionSpec(
                [chunk("old limit ten thousand", {0: 1.0})], status="retired", current=False
            ),
            VersionSpec([chunk("new limit twelve thousand", {0: 1.0, 1: 0.5})]),
        ],
    )
    results = await search(grounded_db, tenant, "what is the limit")
    assert bodies(results) == ["new limit twelve thousand"]
    assert results[0].version_id == document.version_ids[1]
    assert results[0].version_no == 2


async def test_versions_in_other_states_and_deleted_documents_are_never_returned(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    """Every one of these chunks is the nearest possible to the question. Only the visible one may come back."""
    tenant = tenants()
    nearest = {0: 1.0}
    insert_document(
        admin, tenant, "Visible", [VersionSpec([chunk("visible far chunk", {0: 1.0, 5: 4.0})])]
    )
    for status in ("processing", "queued", "failed", "purging"):
        insert_document(
            admin,
            tenant,
            f"{status} version",
            [VersionSpec([chunk(f"hidden {status} chunk", nearest)], status=status, current=False)],
        )
    # Ready, but the document points at another version.
    insert_document(
        admin,
        tenant,
        "Superseded",
        [
            VersionSpec([chunk("hidden ready but not current", nearest)], current=False),
            VersionSpec([chunk("current superseding chunk", {0: 1.0, 6: 4.0})]),
        ],
    )
    insert_document(
        admin,
        tenant,
        "Deleted",
        [VersionSpec([chunk("hidden deleted document chunk", nearest)])],
        deleted=True,
    )
    results = await search(grounded_db, tenant, "chunk", top_k=10)
    assert sorted(bodies(results)) == ["current superseding chunk", "visible far chunk"]


async def test_one_tenant_never_sees_another_tenants_chunks(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    first, second, empty = tenants(), tenants(), tenants()
    for tenant, label in ((first, "alpha"), (second, "beta")):
        insert_document(
            admin,
            tenant,
            f"{label} policy",
            [
                VersionSpec(
                    [
                        chunk(f"{label} shared words", {0: 1.0}),
                        chunk(f"{label} other", {0: 1.0, 1: 1.0}),
                    ]
                )
            ],
        )
    got_first = await search(grounded_db, first, "shared words other")
    got_second = await search(grounded_db, second, "shared words other")
    assert all(c.body.startswith("alpha") for c in got_first) and len(got_first) == 2
    assert all(c.body.startswith("beta") for c in got_second) and len(got_second) == 2
    assert await search(grounded_db, empty, "shared words other") == []


async def test_without_a_tenant_context_nothing_is_returned(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    """Fail closed: a session that never set the tenant sees no chunk."""
    tenant = tenants()
    insert_document(admin, tenant, "Policy", [VersionSpec([chunk("some words here", {0: 1.0})])])
    async with app_sessions(grounded_db) as factory:
        async with factory() as session, session.begin():
            assert await retrieve(session, "some words", as_floats(QUERY), top_k=5) == []


async def test_it_returns_exactly_top_k_rows_when_there_are_enough_and_fewer_when_not(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    tenant, small = tenants(), tenants()
    insert_document(
        admin,
        tenant,
        "Big",
        [VersionSpec([chunk(f"filler number {n}", {0: 1.0, 10 + n: 1.0}) for n in range(8)])],
    )
    insert_document(
        admin,
        small,
        "Small",
        [VersionSpec([chunk(f"filler number {n}", {0: 1.0, 10 + n: 1.0}) for n in range(3)])],
    )
    for k in (1, 5, 8):
        assert len(await search(grounded_db, tenant, "filler", top_k=k)) == k
    assert len(await search(grounded_db, small, "filler", top_k=5)) == 3


async def test_the_keyword_side_can_lift_a_chunk_the_vector_ranks_low(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    tenant = tenants()
    insert_document(
        admin,
        tenant,
        "Policy",
        [
            VersionSpec(
                [
                    chunk("alpha text without the rare word", {0: 1.0, 1: 0.1}),
                    chunk("beta text without it either", {0: 1.0, 1: 0.5}),
                    chunk("gamma text about the zebrafish", {0: 1.0, 1: 3.0}),
                ]
            )
        ],
    )
    results = await search(grounded_db, tenant, "zebrafish")
    assert bodies(results) == [
        "gamma text about the zebrafish",
        "alpha text without the rare word",
        "beta text without it either",
    ]
    lifted, first_by_vector = results[0], results[1]
    assert (lifted.vector_rank, lifted.keyword_rank) == (3, 1)
    assert (first_by_vector.vector_rank, first_by_vector.keyword_rank) == (1, None)


@pytest.mark.parametrize(
    "question",
    [
        "qwertyuiop asdfghjkl",  # words that no chunk holds
        "what is the of and",  # only stop words: no search terms at all
        "????",  # no words
    ],
)
async def test_a_question_with_no_keyword_match_keeps_the_vector_order(
    grounded_db: DbHandle,
    admin: psycopg.Connection,
    tenants: Callable[[], EvalTenant],
    question: str,
) -> None:
    tenant = tenants()
    insert_document(
        admin,
        tenant,
        "Policy",
        [VersionSpec([chunk(f"plain text {n}", {0: 1.0, 1: 0.2 * n}) for n in range(1, 7)])],
    )
    results = await search(grounded_db, tenant, question, top_k=5)
    async with app_sessions(grounded_db) as factory:
        async with begin_tenant_transaction(
            factory, TenantContext(tenant.tenant, tenant.employee)
        ) as session:
            by_vector = await vector_rank(session, as_floats(QUERY), 50)
    assert [(c.version_id, c.position) for c in results] == [
        (row.version_id, row.position) for row in by_vector[:5]
    ]
    assert all(c.keyword_rank is None for c in results)
    assert [c.vector_rank for c in results] == [1, 2, 3, 4, 5]
    # Without a second list the scores strictly fall, so the order is the vector order and not a tie-break.
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == len(scores)


async def test_the_same_question_gives_the_same_order_across_calls_and_across_loads(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    """Equal scores are broken by the md5 of the text, not by random ids: two tenants that load the same documents
    get the same order, even though every id is different."""
    ordered: list[list[str]] = []
    for _ in range(2):
        tenant = tenants()
        # Two documents, identical vectors and identical keyword hits, so every score ties.
        insert_document(admin, tenant, "One", [VersionSpec([chunk("tie body zulu", {0: 1.0})])])
        insert_document(admin, tenant, "Two", [VersionSpec([chunk("tie body alpha", {0: 1.0})])])
        insert_document(admin, tenant, "Three", [VersionSpec([chunk("tie body mike", {0: 1.0})])])
        first = await search(grounded_db, tenant, "tie body")
        second = await search(grounded_db, tenant, "tie body")
        assert bodies(first) == bodies(second)
        ordered.append(bodies(first))
    assert ordered[0] == ordered[1]
    expected = sorted(ordered[0], key=lambda body: hashlib.md5(body.encode()).hexdigest())  # noqa: S324
    assert ordered[0] == expected


async def test_a_deleted_document_is_excluded_as_soon_as_the_delete_commits(
    grounded_db: DbHandle, admin: psycopg.Connection, tenants: Callable[[], EvalTenant]
) -> None:
    """ARCHITECTURE 5.7, test 1, for chunks: no waiting for the purge."""
    tenant = tenants()
    document = insert_document(
        admin,
        tenant,
        "Policy",
        [VersionSpec([chunk("keep me", {0: 1.0}), chunk("drop me", {0: 1.0, 1: 0.1})])],
    )
    results = await search(grounded_db, tenant, "me")
    assert {c.version_id for c in results} == {document.version_ids[0]} and len(results) == 2
    admin.execute(
        "UPDATE grounded.documents SET deleted_at = now(), purge_after = now() + interval '30 days' WHERE id = %s",
        (document.document_id,),
    )
    assert await search(grounded_db, tenant, "me") == []
