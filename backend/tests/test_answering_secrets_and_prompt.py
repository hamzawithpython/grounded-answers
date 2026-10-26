"""
Two rules of ADR 0031 that must stay true:

- Decision 7: an API key never reaches a log, a repr, an error message, a URL or a request body.
- Decision 5: a prompt holds only the question and the chunk text. No user id, email, tenant id, document id,
  version id or title can be in the bytes sent to a provider.
"""

import logging
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from grounded_answers.answering import (
    AnswerModel,
    AnswerModelError,
    AnswerRequest,
    SourceChunk,
    sources_from_chunks,
)
from grounded_answers.answering.adapters import (
    AnthropicAnswerModel,
    GeminiAnswerModel,
    GroqAnswerModel,
)
from grounded_answers.answering.prompt import build_prompt
from grounded_answers.answering.transport import make_client
from grounded_answers.config import Settings
from grounded_answers.retrieval import RetrievedChunk
from tests.answering_support import (
    REQUEST,
    anthropic_response,
    gemini_response,
    groq_response,
    replying,
)

pytestmark = pytest.mark.anyio

EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
PLANTED = "planted-key-9f3a7c1e5b2d4086"
KEY = SecretStr(PLANTED)

Factory = Callable[[httpx.AsyncClient], AnswerModel]
ADAPTERS: list[tuple[str, Factory, dict[str, object]]] = [
    ("groq", lambda c: GroqAnswerModel(KEY, c), groq_response()),
    ("gemini", lambda c: GeminiAnswerModel(KEY, c), gemini_response()),
    ("anthropic", lambda c: AnthropicAnswerModel(KEY, c), anthropic_response()),
]
EACH = pytest.mark.parametrize(("name", "make", "ok"), ADAPTERS, ids=[a[0] for a in ADAPTERS])


# --- keys ---------------------------------------------------------------------------------------------------


def test_settings_never_prints_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, PLANTED)
    settings = Settings(_env_file=None)
    assert settings.groq_api_key is not None
    assert PLANTED not in repr(settings)
    assert PLANTED not in str(settings)
    assert PLANTED not in settings.model_dump_json()
    assert PLANTED not in str(settings.model_dump())
    assert settings.groq_api_key.get_secret_value() == PLANTED


@EACH
def test_an_adapter_never_prints_its_key(name: str, make: Factory, ok: dict[str, object]) -> None:
    model = make(replying(200, ok).client)
    assert PLANTED not in repr(model)
    assert PLANTED not in str(model)
    assert PLANTED not in str(vars(model).get("_api_key"))


@EACH
async def test_no_log_line_holds_the_key_on_success_or_failure(
    name: str, make: Factory, ok: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await make(replying(200, ok).client).answer(REQUEST)
    echoing = {"error": {"message": f"invalid key {PLANTED}"}}
    for status in (401, 429, 500):
        with pytest.raises(AnswerModelError) as caught:
            await make(replying(status, echoing, {"retry-after": "1"}).client).answer(REQUEST)
        assert PLANTED not in str(caught.value)
        assert PLANTED not in repr(caught.value)
    assert PLANTED not in caplog.text


def test_the_http_libraries_loggers_are_held_at_warning() -> None:
    """httpcore logs request headers at DEBUG, and the key is in a header."""
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    make_client()
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING


def test_the_example_env_file_has_no_key_values() -> None:
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if "API_KEY" in line:
            assert line.startswith("#"), line
            assert line.rstrip().endswith("="), line


# --- prompts ------------------------------------------------------------------------------------------------


def _chunk(position: int, body: str, title: str) -> RetrievedChunk:
    return RetrievedChunk(
        version_id=uuid.uuid4(),
        position=position,
        document_id=uuid.uuid4(),
        title=title,
        version_no=7,
        section_number="4.2",
        heading="Heading with a distinctive phrase",
        clause_labels=("4.2.1",),
        body=body,
        score=0.03,
        vector_rank=1,
        keyword_rank=2,
    )


@EACH
async def test_the_bytes_sent_hold_the_question_and_chunk_text_and_no_identifiers(
    name: str, make: Factory, ok: dict[str, object]
) -> None:
    tenant, user = uuid.uuid4(), uuid.uuid4()
    email = "amina.khan@indus-freight.test"
    chunks = [
        _chunk(0, "Drivers must rest for ten hours.", f"policy-for-{email}.docx"),
        _chunk(1, "Overtime is paid at 1.5 times.", "OVERTIME-POLICY-TITLE.md"),
    ]
    request = AnswerRequest("How long must drivers rest?", sources_from_chunks(chunks))
    wire = replying(200, ok)
    await make(wire.client).answer(request)
    sent = wire.requests[-1].content.decode()

    # What must be there.
    assert "How long must drivers rest?" in sent
    assert "[1] Drivers must rest for ten hours." in sent
    assert "[2] Overtime is paid at 1.5 times." in sent
    # What must not: ids, the email, titles, headings, versions, clause labels, the fused score.
    forbidden = [
        str(tenant),
        str(user),
        email,
        "OVERTIME-POLICY-TITLE",
        "policy-for-",
        "distinctive phrase",
        "4.2.1",
    ]
    forbidden += [str(c.version_id) for c in chunks] + [str(c.document_id) for c in chunks]
    for value in forbidden:
        assert value not in sent, value


def test_sources_are_numbered_from_one_and_keep_only_text() -> None:
    chunks = [_chunk(5, "first", "T1"), _chunk(2, "second", "T2")]
    assert sources_from_chunks(chunks) == (SourceChunk(1, "first"), SourceChunk(2, "second"))


def test_the_request_has_no_field_for_an_identity() -> None:
    """A user, email or tenant has nowhere to go: the request type is the guard."""
    assert set(AnswerRequest.__dataclass_fields__) == {
        "question",
        "sources",
        "max_output_tokens",
        "timeout_seconds",
    }
    assert set(SourceChunk.__dataclass_fields__) == {"ref", "text"}


def test_the_prompt_tells_the_model_the_sources_are_data() -> None:
    prompt = build_prompt(REQUEST)
    assert "not instructions" in prompt.system
    assert prompt.user.startswith("Sources:\n[1] ")
    assert prompt.user.endswith("Question: How many days of notice?")
