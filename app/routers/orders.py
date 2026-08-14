from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status

from app.schemas import OrderCreate, OrderResponse
from app.security import resolve_client_ip


router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.post(
    "",
    response_model=OrderResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    summary="Создать заказ",
    responses={
        400: {"description": "Отсутствует корректный Idempotency-Key"},
        409: {"description": "Повторный заказ или конфликт ключа"},
        422: {"description": "Некорректные данные или неизвестный SKU"},
        429: {"description": "Превышен лимит запросов"},
        503: {"description": "Каталог или хранилище недоступны"},
    },
)
def create_order(
    payload: OrderCreate,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> OrderResponse:
    context = request.app.state.context
    client_ip = getattr(request.state, "client_ip", None)
    if client_ip is None:
        peer_ip = request.client.host if request.client else None
        client_ip = resolve_client_ip(
            peer_ip,
            request.headers.get("X-Forwarded-For"),
            context.settings.trusted_proxy_ips,
        )
    outcome = context.order_service.create_order(
        payload,
        idempotency_key=idempotency_key,
        client_ip=client_ip,
        rate_limit_result=getattr(request.state, "rate_limit_result", None),
        rate_limit_error=getattr(request.state, "rate_limit_error", False),
    )
    if outcome.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotency-Replayed"] = "true"
    else:
        response.headers["Idempotency-Replayed"] = "false"
    return outcome.response
