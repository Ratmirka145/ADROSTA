"""CDEK delivery application service and canonical package adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.domain import OrderCalculation
from app.errors import (
    CdekAuthAppError,
    CdekBadResponseAppError,
    CdekLocationNotFoundError,
    CdekNoTariffsError,
    CdekNotConfiguredError,
    CdekOfficeUnavailableError,
    CdekTariffUnavailableError,
    CdekTimeoutAppError,
    CdekUnavailableAppError,
)
from app.integrations import (
    CdekApi,
    CdekAuthError,
    CdekBadResponseError,
    CdekClientError,
    CdekTimeoutError,
    CdekUnavailableError,
)
from app.schemas import (
    CdekCargoResponse,
    CdekCitiesResponse,
    CdekCityResponse,
    CdekDeliveryType,
    CdekOfficeResponse,
    CdekOfficesResponse,
    CdekQuoteRequest,
    CdekQuoteResponse,
    CdekTariffOptionResponse,
    OrderItem,
)


_DELIVERY_MODES = {
    ("door", CdekDeliveryType.DOOR): 1,
    ("door", CdekDeliveryType.PICKUP): 2,
    ("warehouse", CdekDeliveryType.DOOR): 3,
    ("warehouse", CdekDeliveryType.PICKUP): 4,
}


def millimetres_to_cdek_centimetres(value: int) -> int:
    """Convert a positive whole millimetre size to whole cm without understating it."""

    if type(value) is not int or value <= 0:
        raise ValueError("dimension must be a positive integer")
    return (value + 9) // 10


def rubles_to_kopecks(value: object) -> int:
    """Convert CDEK decimal roubles to integer kopecks without binary arithmetic."""

    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid monetary value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid monetary value") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid monetary value")
    try:
        kopecks = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100
        return int(kopecks.to_integral_exact())
    except (InvalidOperation, OverflowError):
        raise ValueError("invalid monetary value") from None


def is_compatible_delivery_mode(
    origin_mode: str,
    delivery_type: CdekDeliveryType,
    delivery_mode: int,
) -> bool:
    """Match official CDEK modes 1..4 to the configured first/last mile scheme."""

    return _DELIVERY_MODES.get((origin_mode, delivery_type)) == delivery_mode


def build_cdek_packages(calculation: OrderCalculation) -> list[dict[str, object]]:
    """Create one CDEK package for every canonical ADROSTA box."""

    packages: list[dict[str, object]] = []
    package_number = 1
    for item in calculation.items:
        for _ in range(item.boxes):
            packages.append(
                {
                    "number": str(package_number),
                    "weight": item.weight_per_box_grams,
                    "length": millimetres_to_cdek_centimetres(item.length_mm),
                    "width": millimetres_to_cdek_centimetres(item.width_mm),
                    "height": millimetres_to_cdek_centimetres(item.height_mm),
                }
            )
            package_number += 1
    return packages


class CdekService:
    def __init__(
        self,
        *,
        client: CdekApi | None,
        from_city_code: int | None,
        origin_mode: str,
        calculate_items: Callable[[Sequence[OrderItem]], OrderCalculation],
    ) -> None:
        self.client = client
        self.from_city_code = from_city_code
        self.origin_mode = origin_mode
        self.calculate_items = calculate_items

    def search_cities(
        self,
        *,
        query: str,
        country_code: str,
        request_id: str | None = None,
    ) -> CdekCitiesResponse:
        client = self._configured_client()
        try:
            raw_items = client.cities(
                query=query,
                country_code=country_code,
                request_id=request_id,
            )
            if not raw_items:
                raise CdekLocationNotFoundError()
            items = [self._city(item) for item in raw_items]
        except CdekClientError as exc:
            self._raise_integration_error(exc)
        return CdekCitiesResponse(items=items)

    def offices(
        self, *, city_code: int, request_id: str | None = None
    ) -> CdekOfficesResponse:
        client = self._configured_client()
        try:
            raw_items = client.delivery_points(
                city_code=city_code,
                request_id=request_id,
            )
            items = [
                self._office(item)
                for item in raw_items
                if item.get("type") == "PVZ"
            ]
        except CdekClientError as exc:
            self._raise_integration_error(exc)
        return CdekOfficesResponse(items=items)

    def quote(
        self, payload: CdekQuoteRequest, *, request_id: str | None = None
    ) -> CdekQuoteResponse:
        calculation = self.calculate_items(payload.items)
        return self.quote_calculation(
            delivery_type=payload.delivery_type,
            to_city_code=payload.to_city_code,
            calculation=calculation,
            request_id=request_id,
        )

    def quote_calculation(
        self,
        *,
        delivery_type: CdekDeliveryType,
        to_city_code: int,
        calculation: OrderCalculation,
        request_id: str | None = None,
    ) -> CdekQuoteResponse:
        """Quote canonical cargo already calculated by the order domain."""

        options = self._quote_options(
            delivery_type=delivery_type,
            to_city_code=to_city_code,
            calculation=calculation,
            request_id=request_id,
        )
        if not options:
            raise CdekNoTariffsError()

        options.sort(
            key=lambda item: (
                item.delivery_amount_kopecks,
                item.period_min_days,
            )
        )
        assert self.from_city_code is not None
        return CdekQuoteResponse(
            delivery_type=delivery_type,
            from_city_code=self.from_city_code,
            to_city_code=to_city_code,
            cargo=CdekCargoResponse(
                total_boxes=calculation.totals.total_boxes,
                total_weight_grams=calculation.totals.total_weight_grams,
                cargo_places=calculation.totals.cargo_places,
            ),
            options=options,
        )

    def verify_selected_tariff(
        self,
        *,
        delivery_type: CdekDeliveryType,
        to_city_code: int,
        tariff_code: int,
        office_code: str | None,
        calculation: OrderCalculation,
        request_id: str | None = None,
    ) -> CdekTariffOptionResponse:
        """Return the current authoritative selected tariff or reject checkout."""

        options = self._quote_options(
            delivery_type=delivery_type,
            to_city_code=to_city_code,
            calculation=calculation,
            request_id=request_id,
        )
        selected = next(
            (option for option in options if option.tariff_code == tariff_code),
            None,
        )
        if selected is None:
            raise CdekTariffUnavailableError()

        if delivery_type is CdekDeliveryType.PICKUP:
            if office_code is None:
                raise CdekOfficeUnavailableError()
            offices = self.offices(city_code=to_city_code, request_id=request_id)
            if not any(
                office.code == office_code and office.city_code == to_city_code
                for office in offices.items
            ):
                raise CdekOfficeUnavailableError()
        return selected

    def _quote_options(
        self,
        *,
        delivery_type: CdekDeliveryType,
        to_city_code: int,
        calculation: OrderCalculation,
        request_id: str | None,
    ) -> list[CdekTariffOptionResponse]:
        client = self._configured_client()
        if self.from_city_code is None:
            raise CdekNotConfiguredError()
        request_payload = {
            "type": 1,
            "currency": 1,
            "lang": "rus",
            "from_location": {"code": self.from_city_code},
            "to_location": {"code": to_city_code},
            "packages": build_cdek_packages(calculation),
        }
        try:
            raw_tariffs = client.tariff_list(request_payload, request_id=request_id)
            options = self._tariffs(raw_tariffs, delivery_type)
        except CdekClientError as exc:
            self._raise_integration_error(exc)
        return options

    def _configured_client(self) -> CdekApi:
        if self.client is None:
            raise CdekNotConfiguredError()
        return self.client

    @staticmethod
    def _raise_integration_error(exc: CdekClientError) -> None:
        if isinstance(exc, CdekTimeoutError):
            raise CdekTimeoutAppError() from None
        if isinstance(exc, CdekAuthError):
            raise CdekAuthAppError() from None
        if isinstance(exc, CdekUnavailableError):
            raise CdekUnavailableAppError() from None
        if isinstance(exc, CdekBadResponseError):
            raise CdekBadResponseAppError() from None
        raise CdekUnavailableAppError() from None

    @classmethod
    def _city(cls, raw: Mapping[str, Any]) -> CdekCityResponse:
        try:
            return CdekCityResponse(
                code=cls._integer(raw.get("code")),
                city=cls._text(raw.get("city")),
                region=cls._optional_text(raw.get("region")),
                country_code=cls._text(raw.get("country_code")).upper(),
            )
        except (ValueError, TypeError):
            raise CdekBadResponseError("CDEK_BAD_RESPONSE") from None

    @classmethod
    def _office(cls, raw: Mapping[str, Any]) -> CdekOfficeResponse:
        location = raw.get("location")
        if not isinstance(location, Mapping):
            raise CdekBadResponseError("CDEK_BAD_RESPONSE")
        try:
            return CdekOfficeResponse(
                code=cls._text(raw.get("code")),
                name=cls._text(raw.get("name")),
                address=cls._text(location.get("address")),
                city_code=cls._integer(location.get("city_code")),
                postal_code=cls._optional_text(location.get("postal_code")),
                latitude=cls._optional_number_text(location.get("latitude")),
                longitude=cls._optional_number_text(location.get("longitude")),
                work_time=cls._optional_text(raw.get("work_time")),
            )
        except (ValueError, TypeError):
            raise CdekBadResponseError("CDEK_BAD_RESPONSE") from None

    def _tariffs(
        self,
        raw_tariffs: Sequence[Mapping[str, Any]],
        delivery_type: CdekDeliveryType,
    ) -> list[CdekTariffOptionResponse]:
        options: list[CdekTariffOptionResponse] = []
        for raw in raw_tariffs:
            try:
                delivery_mode = self._integer(raw.get("delivery_mode"))
                if not is_compatible_delivery_mode(
                    self.origin_mode, delivery_type, delivery_mode
                ):
                    continue
                options.append(
                    CdekTariffOptionResponse(
                        tariff_code=self._integer(raw.get("tariff_code")),
                        tariff_name=self._text(raw.get("tariff_name")),
                        tariff_description=self._optional_text(
                            raw.get("tariff_description")
                        ),
                        delivery_mode=delivery_mode,
                        delivery_amount_kopecks=rubles_to_kopecks(
                            raw.get("delivery_sum")
                        ),
                        period_min_days=self._non_negative_integer(
                            raw.get("period_min")
                        ),
                        period_max_days=self._non_negative_integer(
                            raw.get("period_max")
                        ),
                    )
                )
            except (ValueError, TypeError):
                raise CdekBadResponseError("CDEK_BAD_RESPONSE") from None
        return options

    @staticmethod
    def _integer(value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("expected positive integer")
        return value

    @staticmethod
    def _non_negative_integer(value: object) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("expected non-negative integer")
        return value

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected text")
        return value.strip()

    @classmethod
    def _optional_text(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        return cls._text(value)

    @staticmethod
    def _optional_number_text(value: object) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool) or not isinstance(
            value, (str, int, float, Decimal)
        ):
            raise ValueError("expected coordinate")
        try:
            coordinate = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("expected coordinate") from None
        if not coordinate.is_finite():
            raise ValueError("expected coordinate")
        return str(value)
