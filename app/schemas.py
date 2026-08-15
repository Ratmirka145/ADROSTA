"""Pydantic schemas for the public orders API.

Incoming and outgoing JSON uses camelCase.  Python code uses snake_case.
The request deliberately has no client-controlled cargo metrics: products and
totals are resolved and calculated by the domain layer.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _strip_string(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


_PHONE_ALLOWED = re.compile(r"^[+0-9()\-\.\s]+$")


def normalize_phone(value: Any) -> str:
    """Normalize a Russian 11-digit phone number to ``+7XXXXXXXXXX``.

    Only presentation separators are discarded.  Ten-digit local numbers,
    extensions, letters, ``+8`` and misplaced/multiple plus signs are rejected
    rather than guessed.
    """

    if not isinstance(value, str):
        raise ValueError("phone must be a string")

    raw = value.strip()
    if not raw or _PHONE_ALLOWED.fullmatch(raw) is None:
        raise ValueError("phone contains unsupported characters")
    if "+" in raw and (not raw.startswith("+") or raw.count("+") != 1):
        raise ValueError("plus sign is only allowed once at the beginning")

    digits = "".join(character for character in raw if character in "0123456789")
    if len(digits) != 11 or digits[0] not in {"7", "8"}:
        raise ValueError("phone must contain 11 digits and start with 7 or 8")
    if raw.startswith("+") and digits[0] != "7":
        raise ValueError("an international Russian phone must start with +7")

    return "+7" + digits[1:]


NonEmpty64 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
NonEmpty100 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
NonEmpty200 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
NonEmpty300 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
NonEmpty500 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
Inn = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^(?:\d{10}|\d{12})$"),
]
Kpp = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^\d{9}$"),
]
RussianPhone = Annotated[str, BeforeValidator(normalize_phone)]
NormalizedEmail = Annotated[EmailStr, BeforeValidator(_strip_string)]


class ApiModel(BaseModel):
    """Common strict model configuration for the public API."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class BuyerType(str, Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"


class DeliveryMethod(str, Enum):
    SELF_PICKUP = "self_pickup"
    CDEK = "cdek"


class CdekDeliveryType(str, Enum):
    PICKUP = "pickup"
    DOOR = "door"


class Buyer(ApiModel):
    type: BuyerType
    contact_name: NonEmpty200
    phone: RussianPhone
    email: NormalizedEmail


class Company(ApiModel):
    name: NonEmpty300
    inn: Inn
    kpp: Kpp | None = None
    legal_address: NonEmpty500

    @field_validator("kpp", mode="before")
    @classmethod
    def normalize_empty_kpp(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def validate_kpp_for_inn(self) -> Company:
        if len(self.inn) == 10 and self.kpp is None:
            raise PydanticCustomError(
                "company_kpp_required",
                "kpp is required for a company with a 10-digit INN",
            )
        if len(self.inn) == 12 and self.kpp is not None:
            raise PydanticCustomError(
                "company_kpp_forbidden",
                "kpp must be omitted for an individual entrepreneur",
            )
        return self


class Recipient(ApiModel):
    contact_name: NonEmpty200
    phone: RussianPhone
    email: NormalizedEmail | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_empty_email(cls, value: Any) -> Any:
        return _blank_to_none(value)


class Delivery(ApiModel):
    method: DeliveryMethod
    type: CdekDeliveryType | None = None
    to_city_code: Annotated[StrictInt, Field(gt=0)] | None = None
    tariff_code: Annotated[StrictInt, Field(gt=0)] | None = None
    region: NonEmpty200 | None = None
    city: NonEmpty200 | None = None
    office_code: NonEmpty100 | None = None
    postcode: NonEmpty64 | None = None
    street: NonEmpty500 | None = None
    house: NonEmpty100 | None = None
    apartment: NonEmpty100 | None = None
    recipient: Recipient | None = None

    @field_validator(
        "region",
        "city",
        "office_code",
        "postcode",
        "street",
        "house",
        "apartment",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def validate_delivery_contract(self) -> Delivery:
        cdek_fields = (
            self.type,
            self.to_city_code,
            self.tariff_code,
            self.region,
            self.city,
            self.office_code,
            self.postcode,
            self.street,
            self.house,
            self.apartment,
        )
        if self.method is DeliveryMethod.SELF_PICKUP:
            if any(value is not None for value in cdek_fields):
                raise PydanticCustomError(
                    "delivery_fields_not_allowed",
                    "CDEK fields must be omitted for self pickup",
                )
            return self

        if self.type is None:
            raise PydanticCustomError(
                "cdek_type_required",
                "delivery type is required for CDEK",
            )
        if self.to_city_code is None:
            raise PydanticCustomError(
                "cdek_to_city_code_required",
                "destination city code is required for CDEK",
            )
        if self.tariff_code is None:
            raise PydanticCustomError(
                "cdek_tariff_code_required",
                "selected tariff code is required for CDEK",
            )

        if self.type is CdekDeliveryType.DOOR and self.city is None:
            raise PydanticCustomError(
                "cdek_city_required",
                "city is required for CDEK door delivery",
            )

        if self.type is CdekDeliveryType.PICKUP:
            if self.office_code is None:
                raise PydanticCustomError(
                    "cdek_office_required",
                    "office code is required for CDEK pickup",
                )
            if any(
                value is not None
                for value in (self.postcode, self.street, self.house, self.apartment)
            ):
                raise PydanticCustomError(
                    "cdek_door_fields_not_allowed",
                    "door address fields must be omitted for CDEK pickup",
                )
            return self

        if self.street is None:
            raise PydanticCustomError(
                "cdek_street_required",
                "street is required for CDEK door delivery",
            )
        if self.house is None:
            raise PydanticCustomError(
                "cdek_house_required",
                "house is required for CDEK door delivery",
            )
        if self.office_code is not None:
            raise PydanticCustomError(
                "cdek_office_not_allowed",
                "office code must be omitted for CDEK door delivery",
            )
        return self


class OrderItem(ApiModel):
    sku: NonEmpty64
    boxes: Annotated[StrictInt, Field(ge=1, le=10_000)]


class CartCalculateRequest(ApiModel):
    items: Annotated[list[OrderItem], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def reject_duplicate_skus(self) -> CartCalculateRequest:
        skus = [item.sku for item in self.items]
        if len(skus) != len(set(skus)):
            raise PydanticCustomError(
                "duplicate_sku",
                "items must not contain duplicate SKU values",
            )
        return self


class CdekQuoteRequest(ApiModel):
    delivery_type: CdekDeliveryType
    to_city_code: Annotated[StrictInt, Field(gt=0)]
    items: Annotated[list[OrderItem], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def reject_duplicate_skus(self) -> CdekQuoteRequest:
        skus = [item.sku for item in self.items]
        if len(skus) != len(set(skus)):
            raise PydanticCustomError(
                "duplicate_sku",
                "items must not contain duplicate SKU values",
            )
        return self


class OrderCreateRequest(ApiModel):
    buyer: Buyer
    company: Company | None = None
    delivery: Delivery
    comment: Annotated[str | None, Field(max_length=2_000)] = None
    items: Annotated[list[OrderItem], Field(min_length=1, max_length=100)]

    @field_validator("comment", mode="before")
    @classmethod
    def normalize_comment(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def validate_conditional_fields(self) -> OrderCreateRequest:
        if self.buyer.type is BuyerType.BUSINESS and self.company is None:
            raise PydanticCustomError(
                "company_required",
                "company is required for a business buyer",
            )
        if self.buyer.type is BuyerType.INDIVIDUAL and self.company is not None:
            raise PydanticCustomError(
                "company_forbidden",
                "company must be omitted for an individual buyer",
            )

        skus = [item.sku for item in self.items]
        if len(skus) != len(set(skus)):
            raise PydanticCustomError(
                "duplicate_sku",
                "items must not contain duplicate SKU values",
            )
        return self


# The shorter name is kept as the primary router-facing import.
OrderCreate = OrderCreateRequest


class CalculatedItemResponse(ApiModel):
    sku: str
    name: str
    boxes: int
    units_per_box: int
    units: int
    price_per_unit_kopecks: int
    price_per_box_kopecks: int
    line_amount_kopecks: int
    weight_per_box_grams: int
    total_weight_grams: int
    box_volume_mm3: int
    length_mm: int
    width_mm: int
    height_mm: int
    cargo_places: int
    total_volume_mm3: int


class OrderTotalsResponse(ApiModel):
    total_boxes: int
    total_units: int
    products_amount_kopecks: int
    total_weight_grams: int
    cargo_places: int
    total_volume_mm3: int


class OrderCommercialTotalsResponse(OrderTotalsResponse):
    delivery_amount_kopecks: int
    grand_total_kopecks: int


class OrderDeliveryResponse(ApiModel):
    method: DeliveryMethod
    type: CdekDeliveryType | None = None
    to_city_code: int | None = None
    tariff_code: int | None = None
    tariff_name: str | None = None
    delivery_mode: int | None = None
    office_code: str | None = None
    region: str | None = None
    city: str | None = None
    postcode: str | None = None
    street: str | None = None
    house: str | None = None
    apartment: str | None = None
    period_min_days: int | None = None
    period_max_days: int | None = None


class OrderCalculationResponse(ApiModel):
    items: list[CalculatedItemResponse]
    totals: OrderTotalsResponse

    @classmethod
    def from_domain(cls, calculation: Any) -> OrderCalculationResponse:
        return cls.model_validate(calculation, from_attributes=True)


class CdekCityResponse(ApiModel):
    code: int
    city: str
    region: str | None = None
    country_code: str


class CdekCitiesResponse(ApiModel):
    items: list[CdekCityResponse]


class CdekOfficeResponse(ApiModel):
    code: str
    name: str
    address: str
    city_code: int
    postal_code: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    work_time: str | None = None
    type: Literal["PVZ"] = "PVZ"


class CdekOfficesResponse(ApiModel):
    items: list[CdekOfficeResponse]


class CdekCargoResponse(ApiModel):
    total_boxes: int
    total_weight_grams: int
    cargo_places: int


class CdekTariffOptionResponse(ApiModel):
    tariff_code: int
    tariff_name: NonEmpty500
    tariff_description: str | None = None
    delivery_mode: int
    delivery_amount_kopecks: int
    period_min_days: int
    period_max_days: int


class CdekQuoteResponse(ApiModel):
    delivery_type: CdekDeliveryType
    from_city_code: int
    to_city_code: int
    cargo: CdekCargoResponse
    options: list[CdekTariffOptionResponse]


class OrderResponse(ApiModel):
    order_id: UUID
    status: Literal["accepted"] = "accepted"
    integration_status: Literal["pending", "stored", "delivered", "failed"]
    replayed: bool = False
    items: list[CalculatedItemResponse]
    totals: OrderCommercialTotalsResponse
    delivery: OrderDeliveryResponse

    @classmethod
    def from_domain(
        cls,
        *,
        order_id: UUID,
        calculation: Any,
        integration_status: Literal["pending", "stored", "delivered", "failed"],
        replayed: bool = False,
        delivery: OrderDeliveryResponse | None = None,
        delivery_amount_kopecks: int = 0,
    ) -> OrderResponse:
        rendered = OrderCalculationResponse.from_domain(calculation)
        commercial_totals = OrderCommercialTotalsResponse(
            **rendered.totals.model_dump(),
            delivery_amount_kopecks=delivery_amount_kopecks,
            grand_total_kopecks=(
                rendered.totals.products_amount_kopecks
                + delivery_amount_kopecks
            ),
        )
        return cls(
            order_id=order_id,
            integration_status=integration_status,
            replayed=replayed,
            items=rendered.items,
            totals=commercial_totals,
            delivery=delivery or OrderDeliveryResponse(
                method=DeliveryMethod.SELF_PICKUP
            ),
        )


__all__ = [
    "ApiModel",
    "Buyer",
    "BuyerType",
    "CdekCargoResponse",
    "CdekCitiesResponse",
    "CdekCityResponse",
    "CdekDeliveryType",
    "CdekOfficeResponse",
    "CdekOfficesResponse",
    "CdekQuoteRequest",
    "CdekQuoteResponse",
    "CdekTariffOptionResponse",
    "CartCalculateRequest",
    "CalculatedItemResponse",
    "Company",
    "Delivery",
    "DeliveryMethod",
    "NormalizedEmail",
    "OrderCalculationResponse",
    "OrderCommercialTotalsResponse",
    "OrderCreate",
    "OrderCreateRequest",
    "OrderDeliveryResponse",
    "OrderItem",
    "OrderResponse",
    "OrderTotalsResponse",
    "Recipient",
    "RussianPhone",
    "normalize_phone",
]
