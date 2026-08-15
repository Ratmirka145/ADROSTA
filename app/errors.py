"""Safe, stable API error responses for the ADROSTA backend."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import logging
import re
from typing import Any, Final
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status


logger = logging.getLogger(__name__)

_REQUEST_ID_RE: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FIELD_TOKEN_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_MAX_VALIDATION_DETAILS: Final = 25
_SAFE_HTTP_HEADERS: Final = frozenset(
    {"allow", "retry-after", "www-authenticate"}
)


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNKNOWN_SKU = "UNKNOWN_SKU"
    DUPLICATE_SKU = "DUPLICATE_SKU"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ORDER_IN_PROGRESS = "ORDER_IN_PROGRESS"
    RATE_LIMITED = "RATE_LIMITED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    CATALOG_UNAVAILABLE = "CATALOG_UNAVAILABLE"
    PRICE_TIER_NOT_FOUND = "PRICE_TIER_NOT_FOUND"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    CDEK_NOT_CONFIGURED = "CDEK_NOT_CONFIGURED"
    CDEK_UNAVAILABLE = "CDEK_UNAVAILABLE"
    CDEK_TIMEOUT = "CDEK_TIMEOUT"
    CDEK_AUTH_ERROR = "CDEK_AUTH_ERROR"
    CDEK_BAD_RESPONSE = "CDEK_BAD_RESPONSE"
    CDEK_LOCATION_NOT_FOUND = "CDEK_LOCATION_NOT_FOUND"
    CDEK_NO_TARIFFS = "CDEK_NO_TARIFFS"
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One safe field-level error. It must never contain the rejected value."""

    code: str
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.field:
            result["field"] = self.field
        return result


class AppError(Exception):
    """Expected application failure with a public, non-sensitive response."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Sequence[ErrorDetail] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        normalized_code = code.value if isinstance(code, ErrorCode) else str(code)
        # Exception text intentionally contains only the stable code. This prevents
        # accidental logging of a public message supplied by a caller.
        super().__init__(normalized_code)
        self.code = normalized_code
        self.message = message
        self.status_code = status_code
        self.details = tuple(details)
        self.headers = dict(headers or {})


class ValidationAppError(AppError):
    def __init__(self, details: Sequence[ErrorDetail] = ()) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            "Проверьте правильность заполнения формы.",
            status_code=422,
            details=details,
        )


class UnknownSkuError(AppError):
    def __init__(self, *, field: str = "items") -> None:
        super().__init__(
            ErrorCode.UNKNOWN_SKU,
            "Один из товаров не найден или больше недоступен.",
            status_code=422,
            details=(
                ErrorDetail(
                    field=field,
                    code=ErrorCode.UNKNOWN_SKU.value,
                    message="Проверьте выбранный товар.",
                ),
            ),
        )


class DuplicateSkuError(AppError):
    def __init__(self, *, field: str = "items") -> None:
        super().__init__(
            ErrorCode.DUPLICATE_SKU,
            "Один товар указан в заказе несколько раз.",
            status_code=422,
            details=(
                ErrorDetail(
                    field=field,
                    code=ErrorCode.DUPLICATE_SKU.value,
                    message="Объедините количество коробок для этого товара.",
                ),
            ),
        )


class IdempotencyKeyRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            "Не удалось безопасно отправить заказ. Обновите страницу и попробуйте ещё раз.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )


class DuplicateOrderError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.DUPLICATE_ORDER,
            "Такой заказ уже был принят. Не отправляйте его повторно.",
            status_code=status.HTTP_409_CONFLICT,
        )


class IdempotencyConflictError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "Ключ повторной отправки уже использован для другого заказа.",
            status_code=status.HTTP_409_CONFLICT,
        )


class OrderInProgressError(AppError):
    def __init__(self, *, retry_after: int = 1) -> None:
        retry_after = max(1, int(retry_after))
        super().__init__(
            ErrorCode.ORDER_IN_PROGRESS,
            "Заказ уже обрабатывается. Подождите немного и повторите запрос.",
            status_code=status.HTTP_409_CONFLICT,
            headers={"Retry-After": str(retry_after)},
        )


class RateLimitError(AppError):
    def __init__(self, *, retry_after: int) -> None:
        retry_after = max(1, int(retry_after))
        super().__init__(
            ErrorCode.RATE_LIMITED,
            "Слишком много запросов. Попробуйте снова немного позже.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )


class PayloadTooLargeError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.PAYLOAD_TOO_LARGE,
            "Размер данных заказа превышает допустимый.",
            status_code=413,
        )


class CatalogUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CATALOG_UNAVAILABLE,
            "Каталог товаров временно недоступен. Попробуйте снова позже.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class PriceTierNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.PRICE_TIER_NOT_FOUND,
            "Для товара не настроена цена для выбранного количества коробок.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class UpstreamUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "Сервис отправки заказов временно недоступен. Попробуйте снова позже.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class ServiceUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.SERVICE_UNAVAILABLE,
            "Сервис временно недоступен. Попробуйте снова позже.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class CdekNotConfiguredError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_NOT_CONFIGURED,
            "Расчёт доставки СДЭК пока не настроен.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class CdekUnavailableAppError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_UNAVAILABLE,
            "Сервис СДЭК временно недоступен. Попробуйте снова позже.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class CdekTimeoutAppError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_TIMEOUT,
            "Сервис СДЭК не ответил вовремя. Попробуйте снова позже.",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        )


class CdekAuthAppError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_AUTH_ERROR,
            "Сервис доставки временно недоступен из-за ошибки авторизации.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )


class CdekBadResponseAppError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_BAD_RESPONSE,
            "Сервис СДЭК вернул некорректный ответ.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )


class CdekLocationNotFoundError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_LOCATION_NOT_FOUND,
            "Населённый пункт СДЭК не найден.",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class CdekNoTariffsError(AppError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.CDEK_NO_TARIFFS,
            "Для выбранного направления и груза нет доступных тарифов СДЭК.",
            status_code=422,
        )


def validation_error(details: Sequence[ErrorDetail] = ()) -> ValidationAppError:
    return ValidationAppError(details)


def unknown_sku_error(*, field: str = "items") -> UnknownSkuError:
    return UnknownSkuError(field=field)


def duplicate_sku_error(*, field: str = "items") -> DuplicateSkuError:
    return DuplicateSkuError(field=field)


def idempotency_key_required_error() -> IdempotencyKeyRequiredError:
    return IdempotencyKeyRequiredError()


def duplicate_order_error() -> DuplicateOrderError:
    return DuplicateOrderError()


def idempotency_conflict_error() -> IdempotencyConflictError:
    return IdempotencyConflictError()


def order_in_progress_error(*, retry_after: int = 1) -> OrderInProgressError:
    return OrderInProgressError(retry_after=retry_after)


def rate_limited_error(*, retry_after: int) -> RateLimitError:
    return RateLimitError(retry_after=retry_after)


def catalog_unavailable_error() -> CatalogUnavailableError:
    return CatalogUnavailableError()


def upstream_unavailable_error() -> UpstreamUnavailableError:
    return UpstreamUnavailableError()


def service_unavailable_error() -> ServiceUnavailableError:
    return ServiceUnavailableError()


def _request_id(request: Request) -> str:
    state_value = getattr(request.state, "request_id", None)
    if isinstance(state_value, str) and _REQUEST_ID_RE.fullmatch(state_value):
        return state_value

    header_value = request.headers.get("X-Request-ID", "")
    if _REQUEST_ID_RE.fullmatch(header_value):
        request.state.request_id = header_value
        return header_value

    generated = str(uuid4())
    request.state.request_id = generated
    return generated


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: ErrorCode | str,
    message: str,
    details: Sequence[ErrorDetail] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    normalized_code = code.value if isinstance(code, ErrorCode) else str(code)
    response_headers = {"X-Request-ID": request_id}
    response_headers.update(headers or {})
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": normalized_code,
                "message": message,
                "details": [detail.as_dict() for detail in details],
                "requestId": request_id,
            }
        },
        headers=response_headers,
    )


def _safe_field_path(location: Sequence[Any]) -> str:
    parts = list(location)
    if parts and parts[0] in {"body", "query", "path", "header", "cookie"}:
        parts = parts[1:]

    result = ""
    for part in parts:
        if isinstance(part, int) and part >= 0:
            result += f"[{part}]"
            continue
        if not isinstance(part, str) or not _FIELD_TOKEN_RE.fullmatch(part):
            return "request"
        if part in {"__root__", "root"}:
            return "request"
        result += ("." if result else "") + part
    return result or "request"


def _validation_detail(error: Mapping[str, Any]) -> ErrorDetail:
    error_type = str(error.get("type", ""))
    raw_location = error.get("loc", ())
    location = raw_location if isinstance(raw_location, (tuple, list)) else ()
    field = _safe_field_path(location)

    custom_errors = {
        "company_required": ErrorDetail(
            field="company",
            code="REQUIRED_COMPANY",
            message="Для ООО или ИП заполните реквизиты.",
        ),
        "company_forbidden": ErrorDetail(
            field="company",
            code="COMPANY_NOT_ALLOWED",
            message="Для физического лица реквизиты компании не требуются.",
        ),
        "company_kpp_required": ErrorDetail(
            field="company.kpp",
            code="REQUIRED_KPP",
            message="Для организации с 10-значным ИНН укажите КПП.",
        ),
        "company_kpp_forbidden": ErrorDetail(
            field="company.kpp",
            code="KPP_NOT_ALLOWED",
            message="Для ИП КПП не указывается.",
        ),
        "cdek_type_required": ErrorDetail(
            field="delivery.type",
            code="REQUIRED_CDEK_TYPE",
            message="Для доставки СДЭК выберите способ получения.",
        ),
        "cdek_city_required": ErrorDetail(
            field="delivery.city",
            code="REQUIRED_CITY",
            message="Для доставки СДЭК укажите город.",
        ),
        "cdek_street_required": ErrorDetail(
            field="delivery.street",
            code="REQUIRED_STREET",
            message="Для доставки СДЭК до адреса укажите улицу.",
        ),
        "cdek_house_required": ErrorDetail(
            field="delivery.house",
            code="REQUIRED_HOUSE",
            message="Для доставки СДЭК до адреса укажите номер дома.",
        ),
        "delivery_fields_not_allowed": ErrorDetail(
            field="delivery",
            code="DELIVERY_FIELDS_NOT_ALLOWED",
            message="Для самовывоза данные СДЭК не требуются.",
        ),
        "cdek_door_fields_not_allowed": ErrorDetail(
            field="delivery",
            code="DOOR_FIELDS_NOT_ALLOWED",
            message="Для доставки до ПВЗ не указывайте адрес получателя.",
        ),
        "cdek_office_not_allowed": ErrorDetail(
            field="delivery.officeCode",
            code="OFFICE_CODE_NOT_ALLOWED",
            message="Код ПВЗ нельзя указывать для доставки СДЭК до двери.",
        ),
        "duplicate_sku": ErrorDetail(
            field="items",
            code=ErrorCode.DUPLICATE_SKU.value,
            message="Объедините количество коробок одинакового товара.",
        ),
    }
    if error_type in custom_errors:
        return custom_errors[error_type]

    if error_type == "missing":
        return ErrorDetail(
            field=field,
            code="REQUIRED_FIELD",
            message="Обязательное поле не заполнено.",
        )
    if error_type == "extra_forbidden":
        return ErrorDetail(
            field=field,
            code="UNKNOWN_FIELD",
            message="Поле не поддерживается.",
        )
    if "string_too_short" in error_type or "string_too_long" in error_type:
        return ErrorDetail(
            field=field,
            code="INVALID_LENGTH",
            message="Проверьте длину значения.",
        )
    if "int" in error_type:
        return ErrorDetail(
            field=field,
            code="INVALID_INTEGER",
            message="Укажите целое число.",
        )
    if "bool" in error_type:
        return ErrorDetail(
            field=field,
            code="INVALID_BOOLEAN",
            message="Укажите допустимое логическое значение.",
        )
    if any(
        marker in error_type
        for marker in ("greater_than", "less_than", "too_long", "too_short")
    ):
        return ErrorDetail(
            field=field,
            code="OUT_OF_RANGE",
            message="Значение находится вне допустимого диапазона.",
        )
    return ErrorDetail(
        field=field,
        code="INVALID_VALUE",
        message="Поле заполнено некорректно.",
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    logger.info(
        "application_error request_id=%s code=%s status=%s",
        _request_id(request),
        exc.code,
        exc.status_code,
    )
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Never serialize `exc.errors()` wholesale: Pydantic includes the rejected
    # `input` and may include caller-provided context. Only type and a sanitized
    # schema location are used here.
    details: list[ErrorDetail] = []
    seen: set[tuple[str | None, str]] = set()
    for raw_error in exc.errors()[:_MAX_VALIDATION_DETAILS]:
        detail = _validation_detail(raw_error)
        identity = (detail.field, detail.code)
        if identity not in seen:
            details.append(detail)
            seen.add(identity)

    if not details:
        details.append(
            ErrorDetail(
                field="request",
                code="INVALID_VALUE",
                message="Запрос заполнен некорректно.",
            )
        )
    return _error_response(
        request,
        status_code=422,
        code=ErrorCode.VALIDATION_ERROR,
        message="Проверьте правильность заполнения формы.",
        details=details,
    )


_HTTP_ERROR_RESPONSES: Final[dict[int, tuple[ErrorCode, str]]] = {
    status.HTTP_400_BAD_REQUEST: (
        ErrorCode.BAD_REQUEST,
        "Запрос заполнен некорректно.",
    ),
    status.HTTP_401_UNAUTHORIZED: (
        ErrorCode.UNAUTHORIZED,
        "Требуется авторизация.",
    ),
    status.HTTP_403_FORBIDDEN: (
        ErrorCode.FORBIDDEN,
        "Доступ запрещён.",
    ),
    status.HTTP_404_NOT_FOUND: (
        ErrorCode.NOT_FOUND,
        "Ресурс не найден.",
    ),
    status.HTTP_405_METHOD_NOT_ALLOWED: (
        ErrorCode.METHOD_NOT_ALLOWED,
        "Метод запроса не поддерживается.",
    ),
    413: (
        ErrorCode.PAYLOAD_TOO_LARGE,
        "Размер запроса превышает допустимый.",
    ),
    status.HTTP_429_TOO_MANY_REQUESTS: (
        ErrorCode.RATE_LIMITED,
        "Слишком много запросов. Попробуйте снова немного позже.",
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: (
        ErrorCode.SERVICE_UNAVAILABLE,
        "Сервис временно недоступен. Попробуйте снова позже.",
    ),
}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code, message = _HTTP_ERROR_RESPONSES.get(
        exc.status_code,
        (
            ErrorCode.BAD_REQUEST
            if exc.status_code < 500
            else ErrorCode.INTERNAL_ERROR,
            "Запрос не может быть обработан."
            if exc.status_code < 500
            else "Внутренняя ошибка сервера.",
        ),
    )
    safe_headers = {
        name: value
        for name, value in (exc.headers or {}).items()
        if name.casefold() in _SAFE_HTTP_HEADERS
    }
    return _error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=safe_headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Do not log `str(exc)` or a traceback here: third-party exceptions can embed
    # request bodies, addresses, tokens, or other personal data in their text.
    logger.error(
        "unhandled_exception request_id=%s exception_type=%s",
        _request_id(request),
        type(exc).__name__,
    )
    return _error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code=ErrorCode.INTERNAL_ERROR,
        message="Внутренняя ошибка сервера.",
    )


def install_error_handlers(app: FastAPI) -> None:
    """Install the unified error contract on a FastAPI application."""

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


def register_exception_handlers(app: FastAPI) -> None:
    """Backward-friendly alias for :func:`install_error_handlers`."""

    install_error_handlers(app)


__all__ = [
    "AppError",
    "CatalogUnavailableError",
    "DuplicateSkuError",
    "DuplicateOrderError",
    "ErrorCode",
    "ErrorDetail",
    "IdempotencyConflictError",
    "IdempotencyKeyRequiredError",
    "OrderInProgressError",
    "PayloadTooLargeError",
    "PriceTierNotFoundError",
    "RateLimitError",
    "ServiceUnavailableError",
    "UnknownSkuError",
    "UpstreamUnavailableError",
    "ValidationAppError",
    "app_error_handler",
    "catalog_unavailable_error",
    "duplicate_sku_error",
    "duplicate_order_error",
    "http_exception_handler",
    "idempotency_conflict_error",
    "idempotency_key_required_error",
    "install_error_handlers",
    "order_in_progress_error",
    "rate_limited_error",
    "register_exception_handlers",
    "request_validation_error_handler",
    "service_unavailable_error",
    "unhandled_exception_handler",
    "unknown_sku_error",
    "upstream_unavailable_error",
    "validation_error",
]
