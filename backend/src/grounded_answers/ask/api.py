"""
POST /ask (ADR 0033, decisions 1 and 2): a question in, a checked answer, a refusal, or a reason it was not served out.

The route exists only when ASK_ENABLED is on. The caller comes from `authenticated_principal`, so the tenant and the
user are never taken from the body and no database connection is held while the model is called. The body is the
question and nothing else: 1 to 1,000 characters after trimming, no control characters, no extra fields. A validation
error names the field and the rule, never the value (the app's 422 handler).

Responses (every one has `Cache-Control: no-store` and a `request_id` that appears in the log and nowhere else):
- 200 answered: `status`, `answer`, `citations` (ref, document_id, title, version, clause, clause_kind, heading, quote);
- 200 refused: `status`, `reason` (one of the refusal codes), `message` (ours, never the model's);
- 429: a limit (rate_limited, busy, daily_limit) with `Retry-After`;
- 503: not_configured, try_again or service_unavailable, with `Retry-After` where waiting helps;
- 500: an unexpected error, generic body.
"""

import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grounded_answers.answering.respond import GroundedAnswer
from grounded_answers.ask.messages import (
    INTERNAL_ERROR_MESSAGE,
    LIMIT_MESSAGES,
    UNAVAILABLE_MESSAGES,
)
from grounded_answers.ask.service import AskService, new_request_id
from grounded_answers.ask.types import Answered, AskResult, Caller, Limited, Refused
from grounded_answers.auth_api import AuthenticatedPrincipal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])

MAX_QUESTION_CHARS: Final = 1000
NO_STORE: Final = {"Cache-Control": "no-store"}


class AskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]

    @field_validator("question", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("question", mode="after")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\n\t" for char in value):
            raise ValueError("the question has control characters")
        return value


def _citations(answer: GroundedAnswer) -> list[dict[str, Any]]:
    return [
        {
            "ref": cited.ref,
            "document_id": str(cited.chunk.document_id),
            "title": cited.chunk.title,
            "version": cited.chunk.version_no,
            "clause": cited.location.label,
            "clause_kind": cited.location.level,
            "heading": cited.chunk.heading,
            "quote": cited.quote,
        }
        for cited in answer.citations
    ]


def _respond(status: int, body: dict[str, Any], retry_after: int | None = None) -> JSONResponse:
    headers = dict(NO_STORE)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=status, content=body, headers=headers)


def to_response(result: AskResult) -> JSONResponse:
    if isinstance(result, Answered):
        return _respond(
            200,
            {
                "status": "answered",
                "request_id": result.request_id,
                "answer": result.answer.text,
                "citations": _citations(result.answer),
            },
        )
    if isinstance(result, Refused):
        return _respond(
            200,
            {
                "status": "refused",
                "request_id": result.request_id,
                "reason": result.refusal.kind,
                "message": result.refusal.message,
            },
        )
    if isinstance(result, Limited):
        return _respond(
            429,
            {
                "detail": LIMIT_MESSAGES[result.code],
                "code": result.code,
                "request_id": result.request_id,
            },
            result.retry_after_seconds,
        )
    return _respond(
        503,
        {
            "detail": UNAVAILABLE_MESSAGES[result.code],
            "code": result.code,
            "request_id": result.request_id,
        },
        result.retry_after_seconds,
    )


@router.post("/ask", response_model=None)
async def ask(body: AskBody, request: Request, principal: AuthenticatedPrincipal) -> JSONResponse:
    service: AskService = request.app.state.ask_service
    request_id = new_request_id()
    caller = Caller(tenant_id=principal.tenant_id, user_id=principal.user_id)
    try:
        result = await service.ask(caller, body.question, request_id)
    except Exception:
        # The service has logged the error class and metered the request; the client gets no detail.
        return _respond(500, {"detail": INTERNAL_ERROR_MESSAGE, "request_id": request_id})
    return to_response(result)
