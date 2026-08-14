"""Canonical catalog models and the single trusted order calculator.

All physical measurements describe one box. Money is represented only as
integer kopecks; weights and volumes use integer grams and cubic millimetres.
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


class PriceTierNotFoundError(DomainError):
    def __init__(self, sku: str, boxes: int) -> None:
        self.sku = sku
        self.boxes = boxes
        super().__init__(f"no price tier for SKU {sku} and {boxes} boxes")


class AmbiguousPriceTierError(DomainError):
    def __init__(self, sku: str, boxes: int) -> None:
        self.sku = sku
        self.boxes = boxes
        super().__init__(f"multiple price tiers for SKU {sku} and {boxes} boxes")


class CatalogIntegrityError(RuntimeError):
    """Trusted product data is absent or internally inconsistent."""


def _require_non_empty_string(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CatalogIntegrityError(f"{name} must be a non-empty string")


def _require_positive_integer(name: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise CatalogIntegrityError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class PriceTier:
    min_boxes: int
    max_boxes: int | None
    price_per_unit_kopecks: int

    def __post_init__(self) -> None:
        _require_positive_integer("min_boxes", self.min_boxes)
        if self.max_boxes is not None:
            _require_positive_integer("max_boxes", self.max_boxes)
            if self.max_boxes < self.min_boxes:
                raise CatalogIntegrityError("max_boxes must be at least min_boxes")
        _require_positive_integer(
            "price_per_unit_kopecks", self.price_per_unit_kopecks
        )

    def matches(self, boxes: int) -> bool:
        return boxes >= self.min_boxes and (
            self.max_boxes is None or boxes <= self.max_boxes
        )


@dataclass(frozen=True, slots=True)
class Product:
    """Authoritative product characteristics and SKU-specific price tiers."""

    sku: str
    name: str
    units_per_box: int
    weight_grams: int
    length_mm: int
    width_mm: int
    height_mm: int
    price_tiers: tuple[PriceTier, ...]

    def __post_init__(self) -> None:
        _require_non_empty_string("sku", self.sku)
        _require_non_empty_string("name", self.name)
        _require_positive_integer("units_per_box", self.units_per_box)
        _require_positive_integer("weight_grams", self.weight_grams)
        _require_positive_integer("length_mm", self.length_mm)
        _require_positive_integer("width_mm", self.width_mm)
        _require_positive_integer("height_mm", self.height_mm)
        if not isinstance(self.price_tiers, tuple):
            raise CatalogIntegrityError("price_tiers must be a tuple")
        if any(not isinstance(tier, PriceTier) for tier in self.price_tiers):
            raise CatalogIntegrityError("price_tiers must contain PriceTier values")

    @property
    def volume_mm3(self) -> int:
        return self.length_mm * self.width_mm * self.height_mm


class SupportsOrderItem(Protocol):
    @property
    def sku(self) -> str: ...

    @property
    def boxes(self) -> int: ...


class ProductCatalog(Protocol):
    def get_products(self, skus: Sequence[str]) -> Mapping[str, Product]: ...


@dataclass(frozen=True, slots=True)
class CalculatedOrderItem:
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
    length_mm: int
    width_mm: int
    height_mm: int
    cargo_places: int
    box_volume_mm3: int
    total_volume_mm3: int

    @property
    def weight_kg(self) -> Decimal:
        return Decimal(self.total_weight_grams) / GRAMS_PER_KILOGRAM

    @property
    def volume_m3(self) -> Decimal:
        return Decimal(self.total_volume_mm3) / CUBIC_MILLIMETRES_PER_CUBIC_METRE


@dataclass(frozen=True, slots=True)
class OrderTotals:
    total_boxes: int
    total_units: int
    products_amount_kopecks: int
    total_weight_grams: int
    cargo_places: int
    total_volume_mm3: int

    @property
    def weight_kg(self) -> Decimal:
        return Decimal(self.total_weight_grams) / GRAMS_PER_KILOGRAM

    @property
    def volume_m3(self) -> Decimal:
        return Decimal(self.total_volume_mm3) / CUBIC_MILLIMETRES_PER_CUBIC_METRE


@dataclass(frozen=True, slots=True)
class OrderCalculation:
    items: tuple[CalculatedOrderItem, ...]
    totals: OrderTotals


def _price_for(product: Product, boxes: int) -> int:
    matches = tuple(tier for tier in product.price_tiers if tier.matches(boxes))
    if not matches:
        raise PriceTierNotFoundError(product.sku, boxes)
    if len(matches) > 1:
        raise AmbiguousPriceTierError(product.sku, boxes)
    return matches[0].price_per_unit_kopecks


def calculate_order(
    items: Iterable[SupportsOrderItem],
    products_by_sku: Mapping[str, Product],
) -> OrderCalculation:
    """Calculate every commercial and cargo value from canonical server data."""

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

        price_per_unit = _price_for(product, item.boxes)
        units = item.boxes * product.units_per_box
        box_volume = product.volume_mm3
        calculated_items.append(
            CalculatedOrderItem(
                sku=item.sku,
                name=product.name,
                boxes=item.boxes,
                units_per_box=product.units_per_box,
                units=units,
                price_per_unit_kopecks=price_per_unit,
                price_per_box_kopecks=price_per_unit * product.units_per_box,
                line_amount_kopecks=units * price_per_unit,
                weight_per_box_grams=product.weight_grams,
                total_weight_grams=product.weight_grams * item.boxes,
                length_mm=product.length_mm,
                width_mm=product.width_mm,
                height_mm=product.height_mm,
                cargo_places=item.boxes,
                box_volume_mm3=box_volume,
                total_volume_mm3=box_volume * item.boxes,
            )
        )

    totals = OrderTotals(
        total_boxes=sum(item.boxes for item in calculated_items),
        total_units=sum(item.units for item in calculated_items),
        products_amount_kopecks=sum(
            item.line_amount_kopecks for item in calculated_items
        ),
        total_weight_grams=sum(
            item.total_weight_grams for item in calculated_items
        ),
        cargo_places=sum(item.cargo_places for item in calculated_items),
        total_volume_mm3=sum(
            item.total_volume_mm3 for item in calculated_items
        ),
    )
    return OrderCalculation(items=tuple(calculated_items), totals=totals)


__all__ = [
    "AmbiguousPriceTierError",
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
    "PriceTier",
    "PriceTierNotFoundError",
    "Product",
    "ProductCatalog",
    "SupportsOrderItem",
    "UnknownSkuError",
    "calculate_order",
]
