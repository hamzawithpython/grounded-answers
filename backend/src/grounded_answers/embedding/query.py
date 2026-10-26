"""
The query embedder of the web process (ADR 0033, decision 7; ADR 0030).

The pinned model is loaded in the background after startup, so `/health` answers at once and `/ready` reports 503
`embedder` until the load has finished (or if it fails). A load that fails leaves the process up and unready; the log
line holds the error class, never a path or a message. The model is loaded in a thread, because verifying and loading
it takes seconds and would block the event loop.

Embedding one question is CPU work, so it runs in a thread (`asyncio.to_thread`) behind a semaphore that bounds how
many run at once (EMBED_CONCURRENCY). Two timeouts bound a request: a short one for waiting for a free slot (the
caller answers 503 "busy"), and one for the embedding itself (the caller answers 503 "took too long"). A thread cannot
be cancelled, so a slot is released when its thread finishes, not when the caller gives up: a timed-out request cannot
let more threads run than the limit.

The text of a question is never logged.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Final, Protocol

logger = logging.getLogger(__name__)

LOADING: Final = "loading"
READY: Final = "ready"
FAILED: Final = "failed"

# Time budgets from ADR 0033, decision 6.
SLOT_TIMEOUT_SECONDS: Final = 2.0
EMBED_TIMEOUT_SECONDS: Final = 3.0


class QueryModel(Protocol):
    def query_embedding(self, text: str) -> list[float]: ...


class QueryEmbedderNotReady(RuntimeError):
    """The model is still loading, or its load failed."""


class QueryEmbedderBusy(RuntimeError):
    """No embedding slot became free within the slot timeout."""


class QueryEmbedderTimeout(RuntimeError):
    """The embedding did not finish within the embedding timeout."""


class QueryEmbedder:
    """Loads the model in the background and embeds questions, a bounded number at a time."""

    def __init__(
        self,
        loader: Callable[[], QueryModel],
        *,
        concurrency: int,
        slot_timeout: float = SLOT_TIMEOUT_SECONDS,
        embed_timeout: float = EMBED_TIMEOUT_SECONDS,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._loader = loader
        self._concurrency = concurrency
        self._slot_timeout = slot_timeout
        self._embed_timeout = embed_timeout
        self._slots = asyncio.Semaphore(concurrency)
        self._model: QueryModel | None = None
        self._state = LOADING
        self._load_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def concurrency(self) -> int:
        return self._concurrency

    def start(self) -> None:
        """Begin loading in the background. Returns at once. Call it once, from the running event loop."""
        if self._load_task is not None:
            raise RuntimeError("the query embedder was already started")
        self._load_task = asyncio.create_task(self._load(), name="query-embedder-load")

    async def _load(self) -> None:
        try:
            self._model = await asyncio.to_thread(self._loader)
        except Exception as exc:  # a failed load must become a state, not a lost task
            self._state = FAILED
            logger.error("query embedder load failed reason=%s", type(exc).__name__)
            return
        self._state = READY
        logger.info("query embedder ready concurrency=%d", self._concurrency)

    async def aclose(self) -> None:
        """Stop waiting for the load. A load already running in its thread finishes on its own."""
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._load_task

    async def embed(self, text: str) -> list[float]:
        """
        Embed one question.

        Raises QueryEmbedderNotReady (loading or failed), QueryEmbedderBusy (no slot within the slot timeout) or
        QueryEmbedderTimeout (the embedding took longer than the embedding timeout).
        """
        model = self._model  # set only when the load succeeded, so None means loading or failed
        if model is None:
            raise QueryEmbedderNotReady(self._state)
        try:
            async with asyncio.timeout(self._slot_timeout):
                await self._slots.acquire()
        except TimeoutError:
            raise QueryEmbedderBusy from None
        try:
            work = asyncio.ensure_future(asyncio.to_thread(model.query_embedding, text))
        except BaseException:
            self._slots.release()
            raise
        # The slot belongs to the thread: it is released when the thread ends, even if this caller gave up.
        work.add_done_callback(self._finished)
        try:
            return await asyncio.wait_for(asyncio.shield(work), timeout=self._embed_timeout)
        except TimeoutError:
            raise QueryEmbedderTimeout from None

    def _finished(self, work: "asyncio.Future[list[float]]") -> None:
        self._slots.release()
        if not work.cancelled():
            # Retrieve the result or the error, so an abandoned call never logs "exception was never retrieved".
            error = work.exception()
            if error is not None:
                logger.warning("query embedding failed reason=%s", type(error).__name__)
