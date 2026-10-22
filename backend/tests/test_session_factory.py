"""The session factory keeps loaded attributes after COMMIT (ADR 0005). No database needed."""

from sqlalchemy.ext.asyncio import create_async_engine

from grounded_answers.database import create_session_factory


def test_session_factory_does_not_expire_on_commit() -> None:
    engine = create_async_engine("postgresql+psycopg://app:x@127.0.0.1:5432/db")
    try:
        factory = create_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
        assert factory.kw["bind"] is engine
    finally:
        engine.sync_engine.dispose()
