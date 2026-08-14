"""Pydantic schemas for the public orders API.

Incoming and outgoing JSON uses camelCase.  Python code uses snake_case.
The request deliberately has no client-controlled cargo metrics: products and
totals are resolved and calculated by the domain layer.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
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
    StringConstraints(strip_whitespace=True, pattern=r"^\d{10}$"),
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
    LEGAL = "legal"


class Buyer(ApiModel):
    type: BuyerType
    contact_name: NonEmpty200
    phone: RussianPhone
    email: NormalizedEmail


class Company(ApiModel):
    name: NonEmpty300
    inn: Inn
    kpp: Kpp
    legal_address: NonEmpty500


class Recipient(ApiModel):
    contact_name: NonEmpty200
    phone: RussianPhone
    email: NormalizedEmail | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_empty_email(cls, value: Any) -> Any:
        return _blank_to_none(value)


class Delivery(ApiModel):
    method: NonEmpty100
    transport_company: NonEmpty200 | None = None
    region: NonEmpty200
    city: NonEmpty200
    pickup_point: NonEmpty500 | None = None
    address: NonEmpty500 | None = None
    unloading_required: StrictBool
    access_restrictions: Annotated[str | None, Field(max_length=1_000)] = None
    recipient: Recipient

    @field_validator(
        "transport_company",
        "pickup_point",
        "address",
        "access_restrictions",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def validate_destination(self) -> Delivery:
        if self.pickup_point is None and self.address is None:
            raise PydanticCustomError(
                "delivery_destination_required",
                "pickupPoint or address is required",
            )
        if self.pickup_point is not None and self.address is not None:
            raise PydanticCustomError(
                "delivery_destination_conflict",
                "pickupPoint and address cannot be used together",
            )
        return self


class OrderItem(ApiModel):
    sku: NonEmpty64
    boxes: Annotated[StrictInt, Field(ge=1, le=10_000)]


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
        if self.buyer.type is BuyerType.LEGAL and self.company is None:
            raise PydanticCustomError(
                "company_required",
                "company is required for a legal buyer",
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
    boxes: int
    box_weight_grams: int
    box_volume_mm3: int
    length_mm: int
    width_mm: int
    height_mm: int
    weight_grams: int
    volume_mm3: int
    weight_kg: Decimal
    volume_m3: Decimal


class OrderTotalsResponse(ApiModel):
    boxes: int
    weight_grams: int
    volume_mm3: int
    weight_kg: Decimal
    volume_m3: Decimal


class OrderCalculationResponse(ApiModel):
    items: list[CalculatedItemResponse]
    totals: OrderTotalsResponse

    @classmethod
    def from_domain(cls, calculation: Any) -> OrderCalculationResponse:
        return cls.model_validate(calculation, from_attributes=True)


class OrderResponse(ApiModel):
    order_id: UUID
    status: Literal["accepted"] = "accepted"
    integration_status: Literal["pending", "stored", "delivered", "failed"]
    replayed: bool = False
    items: list[CalculatedItemResponse]
    totals: OrderTotalsResponse

    @classmethod
    def from_domain(
        cls,
        *,
        order_id: UUID,
        calculation: Any,
        integration_status: Literal["pending", "stored", "delivered", "failed"],
        replayed: bool = False,
    ) -> OrderResponse:
        rendered = OrderCalculationResponse.from_domain(calculation)
        return cls(
            order_id=order_id,
            integration_status=integration_status,
            replayed=replayed,
            items=rendered.items,
            totals=rendered.totals,
        )


__all__ = [
    "ApiModel",
    "Buyer",
    "BuyerType",
    "CalculatedItemResponse",
    "Company",
    "Delivery",
    "NormalizedEmail",
    "OrderCalculationResponse",
    "OrderCreate",
    "OrderCreateRequest",
    "OrderItem",
    "OrderResponse",
    "OrderTotalsResponse",
    "Recipient",
    "RussianPhone",
    "normalize_phone",
]
