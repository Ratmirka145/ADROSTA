from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request
from starlette.responses import Response
from starlette.responses import JSONResponse


logger = logging.getLogger("adrosta.http")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Third-party HTTP logs may include a webhook URL. Keep only our
    # metadata-only application logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Add correlation IDs and metadata-only access logs (never request bodies)."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_id
            if _REQUEST_ID_PATTERN.fullmatch(supplied_id)
            else str(uuid4())
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response


class RequestBodyLimitMiddleware:
    """Bound both declared and streamed bodies before application parsing."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw_length = headers.get("content-length")
        if raw_length:
            try:
                too_large = int(raw_length) > self.max_bytes
            except ValueError:
                too_large = False
            if too_large:
                await self._reject(scope, receive, send)
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self.app(scope, _single_message_receive(message), send)
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if body:
                chunks.append(body)
            if not message.get("more_body", False):
                break

        complete_body = b"".join(chunks)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": complete_body,
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.setdefault("state", {})
        request_id = state.get("request_id") or str(uuid4())
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": "Размер данных заказа превышает допустимый.",
                    "details": [],
                    "requestId": request_id,
                }
            },
            headers={"X-Request-ID": request_id},
        )
        await response(scope, receive, send)


def _single_message_receive(message):
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return message
        return {"type": "http.disconnect"}

    return receive
