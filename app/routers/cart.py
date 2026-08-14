from __future__ import annotations

from fastapi import APIRouter, Request

from app.schemas import CartCalculateRequest, OrderCalculationResponse


router = APIRouter(prefix="/api/cart", tags=["cart"])


@router.post(
    "/calculate",
    response_model=OrderCalculationResponse,
    response_model_by_alias=True,
    summary="Рассчитать корзину по серверному каталогу",
    responses={
        422: {"description": "Некорректные данные или неизвестный SKU"},
        503: {"description": "Каталог или price tier недоступен"},
    },
)
def calculate_cart(
    payload: CartCalculateRequest,
    request: Request,
) -> OrderCalculationResponse:
    return request.app.state.context.order_service.calculate_cart(payload)
