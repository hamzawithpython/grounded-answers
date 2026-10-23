"""
The worker checks its database login once at startup (ADR 0022). A wrong password stops the worker with a clear
message that holds no URL and no password. A correct login passes the check and the worker starts normally.
"""

import os

import pytest

from grounded_answers.jobs import worker

from .database import DbHandle

pytestmark = pytest.mark.db

WRONG_CREDENTIAL = "not-the-worker-password-x"


def _worker_url(handle: DbHandle, password: str) -> str:
    return handle.url_for("worker_user", password)


@pytest.mark.anyio
async def test_a_correct_worker_login_passes_the_startup_check(grounded_db: DbHandle) -> None:
    """The check connects as worker_user and runs SELECT 1; a correct login returns without an error."""
    await worker.check_worker_login(
        _worker_url(grounded_db, grounded_db.passwords["worker_user"]).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    )


@pytest.mark.anyio
async def test_a_wrong_worker_password_is_refused_with_no_url_or_password(
    grounded_db: DbHandle,
) -> None:
    with pytest.raises(worker.WorkerLoginFailed) as refused:
        await worker.check_worker_login(
            _worker_url(grounded_db, WRONG_CREDENTIAL).replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        )
    message = str(refused.value)
    assert message == "database login failed for worker_user"
    assert WRONG_CREDENTIAL not in message
    assert grounded_db.passwords["worker_user"] not in message
    assert refused.value.__cause__ is None, (
        "the driver error must not be chained into the traceback"
    )
    assert refused.value.__suppress_context__


def test_main_stops_the_worker_on_a_wrong_password(
    grounded_db: DbHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit message is exactly the one the runbook documents, and the process exits non-zero."""
    model_dir = os.environ.get("TEST_EMBEDDING_MODEL_DIR")
    assert model_dir, (
        "TEST_EMBEDDING_MODEL_DIR is not set (docs/RUNBOOK.md, 'Embedding model files')"
    )
    monkeypatch.setenv("WORKER_DATABASE_URL", _worker_url(grounded_db, WRONG_CREDENTIAL))
    monkeypatch.setenv("WORKER_EMBEDDING_MODEL_DIR", model_dir)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == "worker not started: database login failed for worker_user"
    assert WRONG_CREDENTIAL not in str(stopped.value.code)
