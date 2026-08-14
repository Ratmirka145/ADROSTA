"""Trusted product data and order cargo calculations.

All product measurements are per box.  Calculations stay in integer grams and
cubic millimetres; Decimal kg/m3 values are exact derived representations for
the API response.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Protocol, Sequence


GRAMS_PER_KILOGRAM = Decimal(1_000)
CUBIC_MILLIMETRES_PER_CUBIC_METRE = Decimal(1_000_000_000)
MAX_BOXES_PER_ITEM = 10_000
MAX_ITEMS_PER_ORDER = 100


class DomainError(ValueError):
    """Base class for expected domain validation failures."""


class InvalidOrderItemError(DomainError):
    """An item has an invalid SKU or box count."""


class DuplicateSkuError(DomainError):
    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(f"duplicate SKU in order: {sku}")


class UnknownSkuError(DomainError):
    def __init__(self, skus: Sequence[str]) -> None:
        self.skus = tuple(dict.fromkeys(skus))
        super().__init__(f"unknown SKU: {', '.join(self.skus)}")


class CatalogIntegrityError(RuntimeError):
    """Trusted product data is absent or internally inconsistent."""


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_positive_integer(name: str, value: object) -> None:
    # bool is an int subclass, but is never a valid measurement or quantity.
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class Product:
    """Authoritative per-box product characteristics."""

    sku: str
    weight_grams: int
    length_mm: int
    width_mm: int
    height_mm: int

    def __post_init__(self) -> None:
        _require_non_empty_string("sku", self.sku)
        _require_positive_integer("weight_grams", self.weight_grams)
        _require_positive_integer("length_mm", self.length_mm)
        _require_positive_integer("width_mm", self.width_mm)
        _require_positive_integer("height_mm", self.height_mm)

    @property
    def volume_mm3(self) -> int:
        return self.length_mm * self.width_mm * self.height_mm


class SupportsOrderItem(Protocol):
    """Structural input accepted by :func:`calculate_order`."""

    @property
    def sku(self) -> str: ...

    @property
    def boxes(self) -> int: ...


class ProductCatalog(Protocol):
    """Interface implemented by a SQLite or trusted Python catalog."""

    def get_products(self, skus: Sequence[str]) -> Mapping[str, Product]: ...


@dataclass(frozen=True, slots=True)
class CalculatedOrderItem:
    sku: str
    boxes: int
    box_weight_grams: int
    box_volume_mm3: int
    length_mm: int
    width_mm: int
    height_mm: int
    weight_grams: int
    volume_mm3: int

    @property
    def weight_kg(self) -> Decimal:
        return Decimal(self.weight_grams) / GRAMS_PER_KILOGRAM

    @property
    def volume_m3(self) -> Decimal:
        return Decimal(self.volume_mm3) / CUBIC_MILLIMETRES_PER_CUBIC_METRE


@dataclass(frozen=True, slots=True)
class OrderTotals:
    boxes: int
    weight_grams: int
    volume_mm3: int

    @property
    def weight_kg(self) -> Decimal:
        return Decimal(self.weight_grams) / GRAMS_PER_KILOGRAM

    @property
    def volume_m3(self) -> Decimal:
        return Decimal(self.volume_mm3) / CUBIC_MILLIMETRES_PER_CUBIC_METRE


@dataclass(frozen=True, slots=True)
class OrderCalculation:
    items: tuple[CalculatedOrderItem, ...]
    totals: OrderTotals


def calculate_order(
    items: Iterable[SupportsOrderItem],
    products_by_sku: Mapping[str, Product],
) -> OrderCalculation:
    """Calculate trusted order metrics from requested boxes and catalog data.

    ``items`` may contain Pydantic ``OrderItem`` values directly.  SKU lookup
    is exact and case-sensitive.  The function never accepts client-provided
    measurements.
    """

    requested_items = tuple(items)
    if not requested_items:
        raise InvalidOrderItemError("an order must contain at least one item")
    if len(requested_items) > MAX_ITEMS_PER_ORDER:
        raise InvalidOrderItemError(
            f"an order may contain at most {MAX_ITEMS_PER_ORDER} items"
        )

    seen_skus: set[str] = set()
    unknown_skus: list[str] = []

    for item in requested_items:
        if not isinstance(item.sku, str) or not item.sku.strip():
            raise InvalidOrderItemError("item SKU must be a non-empty string")
        if type(item.boxes) is not int or not 1 <= item.boxes <= MAX_BOXES_PER_ITEM:
            raise InvalidOrderItemError(
                f"boxes must be an integer between 1 and {MAX_BOXES_PER_ITEM}"
            )
        if item.sku in seen_skus:
            raise DuplicateSkuError(item.sku)
        seen_skus.add(item.sku)
        if item.sku not in products_by_sku:
            unknown_skus.append(item.sku)

    if unknown_skus:
        raise UnknownSkuError(unknown_skus)

    calculated_items: list[CalculatedOrderItem] = []
    for item in requested_items:
        product = products_by_sku[item.sku]
        if not isinstance(product, Product):
            raise CatalogIntegrityError(f"catalog entry for {item.sku} is not a Product")
        if product.sku != item.sku:
            raise CatalogIntegrityError(
                f"catalog key {item.sku} does not match product SKU {product.sku}"
            )

        box_volume_mm3 = product.volume_mm3
        calculated_items.append(
            CalculatedOrderItem(
                sku=item.sku,
                boxes=item.boxes,
                box_weight_grams=product.weight_grams,
                box_volume_mm3=box_volume_mm3,
                length_mm=product.length_mm,
                width_mm=product.width_mm,
                height_mm=product.height_mm,
                weight_grams=product.weight_grams * item.boxes,
                volume_mm3=box_volume_mm3 * item.boxes,
            )
        )

    totals = OrderTotals(
        boxes=sum(item.boxes for item in calculated_items),
        weight_grams=sum(item.weight_grams for item in calculated_items),
        volume_mm3=sum(item.volume_mm3 for item in calculated_items),
    )
    return OrderCalculation(items=tuple(calculated_items), totals=totals)


__all__ = [
    "CUBIC_MILLIMETRES_PER_CUBIC_METRE",
    "CalculatedOrderItem",
    "CatalogIntegrityError",
    "DomainError",
    "DuplicateSkuError",
    "GRAMS_PER_KILOGRAM",
    "InvalidOrderItemError",
    "MAX_BOXES_PER_ITEM",
    "MAX_ITEMS_PER_ORDER",
    "OrderCalculation",
    "OrderTotals",
    "Product",
    "ProductCatalog",
    "SupportsOrderItem",
    "UnknownSkuError",
    "calculate_order",
]
