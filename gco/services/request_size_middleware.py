"""Shared request-body size enforcement for GCO FastAPI services."""

from __future__ import annotations

from collections import deque

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


class RequestSizeLimitMiddleware:
    """Reject request bodies larger than the configured byte limit.

    ``Content-Length`` is only an early-rejection optimization. Every accepted
    request is read from the ASGI receive channel and counted before it reaches
    authentication or a route handler, so a missing, malformed, negative, or
    deliberately under-reported header cannot bypass the limit. Buffered
    messages are replayed unchanged to preserve the exact bytes used by HMAC
    validation and downstream parsing.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES) -> None:
        if max_body_bytes < 0:
            raise ValueError("max_body_bytes must be non-negative")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET") in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        if self._declared_size_exceeds_limit(scope):
            await self._too_large_response()(scope, receive, send)
            return

        buffered_messages: deque[Message] = deque()
        received_bytes = 0
        while True:
            message = await receive()
            message_type = message["type"]

            if message_type == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    await self._too_large_response()(scope, receive, send)
                    return
                buffered_messages.append(message)
                if not message.get("more_body", False):
                    break
            else:
                buffered_messages.append(message)
                if message_type == "http.disconnect":
                    break

        async def replay_receive() -> Message:
            if buffered_messages:
                return buffered_messages.popleft()
            return await receive()

        await self.app(scope, replay_receive, send)

    def _declared_size_exceeds_limit(self, scope: Scope) -> bool:
        """Return true if any valid Content-Length value is already too large."""
        for name, raw_value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                declared_size = int(raw_value.decode("ascii"))
            except UnicodeDecodeError, ValueError:
                continue
            if declared_size > self.max_body_bytes:
                return True
        return False

    def _too_large_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={"detail": f"Request body exceeds maximum size of {self.max_body_bytes} bytes"},
        )
