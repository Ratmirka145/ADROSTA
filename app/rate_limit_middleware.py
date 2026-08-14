from __future__ import annotations

import sqlite3

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.responses import JSONResponse

from app.security import resolve_client_ip


class OrderRateLimitMiddleware(BaseHTTPMiddleware):
    """Count every order attempt before body parsing, including invalid bodies."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        applies = request.method == "POST" and request.url.path == "/api/orders"
        rate_result = None
        if applies:
            context = request.app.state.context
            peer_ip = request.client.host if request.client else None
            client_ip = resolve_client_ip(
                peer_ip,
                request.headers.get("X-Forwarded-For"),
                context.settings.trusted_proxy_ips,
            )
            request.state.client_ip = client_ip
            if context.rate_limits is not None:
                try:
                    rate_result = context.rate_limits.consume(
                        client_ip,
                        limit=context.settings.order_rate_limit_count,
                        window_seconds=context.settings.order_rate_limit_window_seconds,
                    )
                    request.state.rate_limit_result = rate_result
                except sqlite3.Error:
                    request.state.rate_limit_error = True
                    return self._error_response(
                        request,
                        status_code=503,
                        code="SERVICE_UNAVAILABLE",
                        message="Сервис временно недоступен. Попробуйте снова позже.",
                    )
        response = await call_next(request)
        if (
            applies
            and rate_result is not None
            and not rate_result.allowed
            and response.status_code not in {200, 429}
        ):
            return self._error_response(
                request,
                status_code=429,
                code="RATE_LIMITED",
                message="Слишком много запросов. Попробуйте снова немного позже.",
                retry_after=max(1, rate_result.retry_after_seconds),
            )
        return response

    @staticmethod
    def _error_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        message: str,
        retry_after: int | None = None,
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unavailable")
        headers = {"X-Request-ID": request_id}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": [],
                    "requestId": request_id,
                }
            },
            headers=headers,
        )
