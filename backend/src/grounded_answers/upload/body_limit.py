"""
Cap the upload request body while it streams, before any parser runs (ADR 0019, decision D2).

FastAPI and Starlette parse a multipart body (and may spool a file part to disk) before the endpoint
runs, so a check in the endpoint is too late. This ASGI middleware sits in front of the request:
- a Content-Length above the cap is refused at once, without reading the body;
- otherwise every chunk is counted as it is received, chunked bodies included, and the request is
  stopped with 413 when the count passes the cap. Whatever the client claimed is then irrelevant.

Only the upload path is limited. Other routes are not touched.
"""

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_TOO_LARGE_BODY: Final = json.dumps({"detail": "The request is too large"}).encode()


class _BodyTooLarge(Exception):
    """Raised inside receive once the cap is passed. It never leaves the middleware."""


class UploadBodyLimit:
    """ASGI middleware: refuse an oversized request body on one path, while it streams."""

    def __init__(self, app: ASGIApp, *, path: str, max_bytes: int) -> None:
        self.app = app
        self.path = path
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._limited(scope):
            await self.app(scope, receive, send)
            return
        if self._declared_too_large(scope):
            await _send_413(send)
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_413(send)

    def _limited(self, scope: Scope) -> bool:
        return bool(
            scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == self.path
        )

    def _declared_too_large(self, scope: Scope) -> bool:
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    return bool(int(value) > self.max_bytes)
                except ValueError:
                    return True
        return False


async def _send_413(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})
