from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from starlette.responses import Response as StarletteResponse

from app.config import Settings
from app.errors import OrderNotAvailableError
from app.repositories import CustomerSessionIssue
from app.schemas import CustomerOrderResponse


router = APIRouter(prefix="/api/customer", tags=["customer"])


def set_customer_session_cookie(
    response: Response,
    settings: Settings,
    issue: CustomerSessionIssue,
) -> None:
    response.set_cookie(
        key=settings.customer_session_cookie_name,
        value=issue.token,
        max_age=settings.customer_session_ttl_seconds,
        expires=datetime.fromtimestamp(issue.expires_at, UTC),
        path=settings.customer_session_cookie_path,
        secure=settings.customer_session_cookie_secure,
        httponly=True,
        samesite=cast(
            Literal["lax", "strict", "none"],
            settings.customer_session_cookie_samesite,
        ),
    )


def clear_customer_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.customer_session_cookie_name,
        path=settings.customer_session_cookie_path,
        secure=settings.customer_session_cookie_secure,
        httponly=True,
        samesite=cast(
            Literal["lax", "strict", "none"],
            settings.customer_session_cookie_samesite,
        ),
    )


@router.get(
    "/orders/{order_number}",
    response_model=CustomerOrderResponse,
    response_model_by_alias=True,
    summary="Получить свой заказ",
    responses={
        404: {"description": "Заказ недоступен"},
        503: {"description": "Хранилище временно недоступно"},
    },
)
def get_customer_order(
    order_number: str,
    request: Request,
) -> CustomerOrderResponse:
    context = request.app.state.context
    token = request.cookies.get(context.settings.customer_session_cookie_name)
    order, _ = context.customer_access_service.get_order(token, order_number)
    if order is None:
        raise OrderNotAvailableError()
    return order


@router.get(
    "/orders/{order_number}/invoice.pdf",
    summary="Получить PDF-счёт своего заказа",
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"description": "Заказ или счёт недоступен"},
        409: {"description": "Счёт владельца заказа ещё не готов"},
        503: {"description": "Хранилище временно недоступно"},
    },
)
def get_customer_invoice_pdf(
    order_number: str,
    request: Request,
    disposition: Annotated[
        Literal["inline", "attachment"],
        Query(),
    ] = "attachment",
) -> StarletteResponse:
    context = request.app.state.context
    token = request.cookies.get(context.settings.customer_session_cookie_name)
    invoice, _ = context.customer_access_service.get_invoice_pdf(
        token,
        order_number,
    )
    if invoice is None:
        raise OrderNotAvailableError()
    filename = f"adrosta-invoice-{invoice.order_number}.pdf"
    return StarletteResponse(
        content=invoice.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/session/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Завершить customer session",
)
def logout_customer_session(request: Request, response: Response) -> None:
    context = request.app.state.context
    origin = request.headers.get("Origin")
    if origin is None or origin not in context.settings.cors_allowed_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    token = request.cookies.get(context.settings.customer_session_cookie_name)
    context.customer_access_service.logout(token)
    clear_customer_session_cookie(response, context.settings)
