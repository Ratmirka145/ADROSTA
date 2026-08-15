from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.errors import ErrorDetail, ValidationAppError
from app.schemas import (
    CdekCitiesResponse,
    CdekOfficesResponse,
    CdekQuoteRequest,
    CdekQuoteResponse,
)


router = APIRouter(prefix="/api/delivery/cdek", tags=["delivery-cdek"])


@router.get(
    "/cities",
    response_model=CdekCitiesResponse,
    response_model_by_alias=True,
    summary="Найти города в справочнике СДЭК",
    responses={
        404: {"description": "Город не найден"},
        502: {"description": "Некорректный ответ или ошибка авторизации СДЭК"},
        503: {"description": "Интеграция не настроена или СДЭК недоступен"},
        504: {"description": "Таймаут СДЭК"},
    },
)
def search_cities(
    request: Request,
    query: Annotated[str, Query(min_length=1, max_length=100)],
    country_code: Annotated[
        str,
        Query(alias="countryCode", min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$"),
    ] = "RU",
) -> CdekCitiesResponse:
    normalized_query = query.strip()
    if len(normalized_query) < 2:
        raise ValidationAppError(
            (
                ErrorDetail(
                    code="INVALID_LENGTH",
                    field="query",
                    message="Введите не менее двух символов названия города.",
                ),
            )
        )
    return request.app.state.context.cdek_service.search_cities(
        query=normalized_query,
        country_code=country_code.upper(),
        request_id=getattr(request.state, "request_id", None),
    )


@router.get(
    "/offices",
    response_model=CdekOfficesResponse,
    response_model_by_alias=True,
    summary="Получить обычные ПВЗ СДЭК в городе",
    responses={
        502: {"description": "Некорректный ответ или ошибка авторизации СДЭК"},
        503: {"description": "Интеграция не настроена или СДЭК недоступен"},
        504: {"description": "Таймаут СДЭК"},
    },
)
def offices(
    request: Request,
    city_code: Annotated[int, Query(alias="cityCode", gt=0)],
) -> CdekOfficesResponse:
    return request.app.state.context.cdek_service.offices(
        city_code=city_code,
        request_id=getattr(request.state, "request_id", None),
    )


@router.post(
    "/quote",
    response_model=CdekQuoteResponse,
    response_model_by_alias=True,
    summary="Рассчитать доступные тарифы доставки СДЭК",
    responses={
        422: {"description": "Некорректная корзина или нет совместимых тарифов"},
        502: {"description": "Некорректный ответ или ошибка авторизации СДЭК"},
        503: {"description": "Интеграция не настроена или СДЭК недоступен"},
        504: {"description": "Таймаут СДЭК"},
    },
)
def quote(payload: CdekQuoteRequest, request: Request) -> CdekQuoteResponse:
    return request.app.state.context.cdek_service.quote(
        payload,
        request_id=getattr(request.state, "request_id", None),
    )

