"""SQLAlchemy repositories for products, orders, limits and delivery events.

Only trusted product snapshots and normalized order fields are stored. Raw
idempotency keys, client/IP identifiers and request fingerprints are persisted
only as domain-separated HMAC-SHA256 digests.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Database
from app.domain import (
    OrderCalculation,
    PriceTier as DomainPriceTier,
    Product as DomainProduct,
)
from app.models import (
    IdempotencyRecordModel,
    OrderItemModel,
    OrderModel,
    OutboxModel,
    ProductModel,
    ProductPriceTierModel,
    RateLimitWindowModel,
)


_MAX_BIGINT = 9_223_372_036_854_775_807
_PRODUCT_UPDATE_FIELDS = {
    "name",
    "units_per_box",
    "box_weight_grams",
    "box_volume_mm3",
    "box_length_mm",
    "box_width_mm",
    "box_height_mm",
    "active",
}


class RepositoryError(Exception):
    """Base exception for expected persistence failures."""


class ProductAlreadyExistsError(RepositoryError):
    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(f"Product already exists: {sku}")


class ProductNotFoundError(RepositoryError):
    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(f"Product not found: {sku}")


class UnknownProductError(RepositoryError):
    def __init__(self, skus: Iterable[str]) -> None:
        self.skus = tuple(sorted(set(skus)))
        super().__init__("Unknown or inactive SKU: " + ", ".join(self.skus))


class ProductCatalogError(RepositoryError):
    """Trusted product data is incomplete or internally inconsistent."""


class InvalidOrderError(RepositoryError):
    pass


class IdempotencyConflictError(RepositoryError):
    """The same idempotency key was reused for a different request."""


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    box_weight_grams: int
    box_volume_mm3: int
    box_length_mm: Optional[int] = None
    box_width_mm: Optional[int] = None
    box_height_mm: Optional[int] = None
    units_per_box: int = 1
    active: bool = True
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


ProductPriceTier = DomainPriceTier


@dataclass(frozen=True)
class OrderItemInput:
    sku: str
    boxes: int


@dataclass(frozen=True)
class OrderDraft:
    buyer_type: str
    buyer_contact_name: str
    buyer_phone: str
    buyer_email: str
    delivery_method: str
    items: Sequence[OrderItemInput]
    company_name: Optional[str] = None
    company_inn: Optional[str] = None
    company_kpp: Optional[str] = None
    company_legal_address: Optional[str] = None
    delivery_type: Optional[str] = None
    cdek_to_city_code: Optional[int] = None
    cdek_tariff_code: Optional[int] = None
    cdek_tariff_name: Optional[str] = None
    cdek_delivery_mode: Optional[int] = None
    cdek_period_min_days: Optional[int] = None
    cdek_period_max_days: Optional[int] = None
    delivery_amount_kopecks: int = 0
    delivery_region: Optional[str] = None
    delivery_city: Optional[str] = None
    delivery_office_code: Optional[str] = None
    delivery_postcode: Optional[str] = None
    delivery_street: Optional[str] = None
    delivery_house: Optional[str] = None
    delivery_apartment: Optional[str] = None
    recipient_contact_name: Optional[str] = None
    recipient_phone: Optional[str] = None
    recipient_email: Optional[str] = None
    comment: Optional[str] = None


@dataclass(frozen=True)
class OrderItemSnapshot:
    sku: str
    product_name: str
    boxes: int
    units_per_box: int
    units: int
    price_per_unit_kopecks: int
    price_per_box_kopecks: int
    line_amount_kopecks: int
    unit_weight_grams: int
    unit_volume_mm3: int
    total_weight_grams: int
    total_volume_mm3: int
    cargo_places: int
    box_length_mm: Optional[int] = None
    box_width_mm: Optional[int] = None
    box_height_mm: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.product_name,
            "boxes": self.boxes,
            "unitsPerBox": self.units_per_box,
            "units": self.units,
            "pricePerUnitKopecks": self.price_per_unit_kopecks,
            "pricePerBoxKopecks": self.price_per_box_kopecks,
            "lineAmountKopecks": self.line_amount_kopecks,
            "unitWeightGrams": self.unit_weight_grams,
            "unitVolumeMm3": self.unit_volume_mm3,
            "totalWeightGrams": self.total_weight_grams,
            "totalVolumeMm3": self.total_volume_mm3,
            "cargoPlaces": self.cargo_places,
            "dimensionsMm": (
                {
                    "length": self.box_length_mm,
                    "width": self.box_width_mm,
                    "height": self.box_height_mm,
                }
                if self.box_length_mm is not None
                else None
            ),
        }


@dataclass(frozen=True)
class OrderResponse:
    order_id: str
    status: str
    created_at: int
    total_boxes: int
    total_units: int
    products_amount_kopecks: int
    delivery_amount_kopecks: int
    grand_total_kopecks: int
    total_weight_grams: int
    total_volume_mm3: int
    cargo_places: int
    delivery_method: str
    delivery_type: Optional[str]
    cdek_to_city_code: Optional[int]
    cdek_tariff_code: Optional[int]
    cdek_tariff_name: Optional[str]
    cdek_delivery_mode: Optional[int]
    cdek_period_min_days: Optional[int]
    cdek_period_max_days: Optional[int]
    delivery_region: Optional[str]
    delivery_city: Optional[str]
    delivery_office_code: Optional[str]
    delivery_postcode: Optional[str]
    delivery_street: Optional[str]
    delivery_house: Optional[str]
    delivery_apartment: Optional[str]
    items: Tuple[OrderItemSnapshot, ...]

    def as_dict(self) -> dict:
        delivery = {"method": self.delivery_method}
        for name, value in (
            ("type", self.delivery_type),
            ("toCityCode", self.cdek_to_city_code),
            ("tariffCode", self.cdek_tariff_code),
            ("tariffName", self.cdek_tariff_name),
            ("deliveryMode", self.cdek_delivery_mode),
            ("officeCode", self.delivery_office_code),
            ("region", self.delivery_region),
            ("city", self.delivery_city),
            ("postcode", self.delivery_postcode),
            ("street", self.delivery_street),
            ("house", self.delivery_house),
            ("apartment", self.delivery_apartment),
            ("periodMinDays", self.cdek_period_min_days),
            ("periodMaxDays", self.cdek_period_max_days),
        ):
            if value is not None:
                delivery[name] = value
        return {
            "orderId": self.order_id,
            "status": self.status,
            "createdAt": self.created_at,
            "totals": {
                "boxes": self.total_boxes,
                "totalUnits": self.total_units,
                "productsAmountKopecks": self.products_amount_kopecks,
                "deliveryAmountKopecks": self.delivery_amount_kopecks,
                "grandTotalKopecks": self.grand_total_kopecks,
                "weightGrams": self.total_weight_grams,
                "volumeMm3": self.total_volume_mm3,
                "cargoPlaces": self.cargo_places,
            },
            "delivery": delivery,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class CreateOrderResult:
    response: OrderResponse
    created: bool
    replayed: bool
    duplicate: bool

    @property
    def disposition(self) -> str:
        if self.created:
            return "created"
        if self.replayed:
            return "replayed"
        return "duplicate"


@dataclass(frozen=True)
class WebhookOrder:
    order_id: str
    status: str
    created_at: int
    buyer_type: str
    buyer_contact_name: str
    buyer_phone: str
    buyer_email: str
    company_name: Optional[str]
    company_inn: Optional[str]
    company_kpp: Optional[str]
    company_legal_address: Optional[str]
    delivery_method: str
    delivery_type: Optional[str]
    cdek_to_city_code: Optional[int]
    cdek_tariff_code: Optional[int]
    cdek_tariff_name: Optional[str]
    cdek_delivery_mode: Optional[int]
    cdek_period_min_days: Optional[int]
    cdek_period_max_days: Optional[int]
    delivery_region: Optional[str]
    delivery_city: Optional[str]
    delivery_office_code: Optional[str]
    delivery_postcode: Optional[str]
    delivery_street: Optional[str]
    delivery_house: Optional[str]
    delivery_apartment: Optional[str]
    recipient_contact_name: Optional[str]
    recipient_phone: Optional[str]
    recipient_email: Optional[str]
    comment: Optional[str]
    total_boxes: int
    total_units: int
    products_amount_kopecks: int
    delivery_amount_kopecks: int
    grand_total_kopecks: int
    total_weight_grams: int
    total_volume_mm3: int
    cargo_places: int
    items: Tuple[OrderItemSnapshot, ...]

    def as_dict(self) -> dict:
        company = None
        if any(
            value is not None
            for value in (
                self.company_name,
                self.company_inn,
                self.company_kpp,
                self.company_legal_address,
            )
        ):
            company = {
                "name": self.company_name,
                "inn": self.company_inn,
                "kpp": self.company_kpp,
                "legalAddress": self.company_legal_address,
            }
        delivery = {"method": self.delivery_method}
        for name, value in (
            ("type", self.delivery_type),
            ("toCityCode", self.cdek_to_city_code),
            ("tariffCode", self.cdek_tariff_code),
            ("tariffName", self.cdek_tariff_name),
            ("deliveryMode", self.cdek_delivery_mode),
            ("region", self.delivery_region),
            ("city", self.delivery_city),
            ("officeCode", self.delivery_office_code),
            ("postcode", self.delivery_postcode),
            ("street", self.delivery_street),
            ("house", self.delivery_house),
            ("apartment", self.delivery_apartment),
            ("periodMinDays", self.cdek_period_min_days),
            ("periodMaxDays", self.cdek_period_max_days),
        ):
            if value is not None:
                delivery[name] = value
        if any(
            value is not None
            for value in (
                self.recipient_contact_name,
                self.recipient_phone,
                self.recipient_email,
            )
        ):
            delivery["recipient"] = {
                "contactName": self.recipient_contact_name,
                "phone": self.recipient_phone,
                "email": self.recipient_email,
            }
        return {
            "orderId": self.order_id,
            "status": self.status,
            "createdAt": self.created_at,
            "buyer": {
                "type": self.buyer_type,
                "contactName": self.buyer_contact_name,
                "phone": self.buyer_phone,
                "email": self.buyer_email,
            },
            "company": company,
            "delivery": delivery,
            "comment": self.comment,
            "totals": {
                "boxes": self.total_boxes,
                "totalUnits": self.total_units,
                "productsAmountKopecks": self.products_amount_kopecks,
                "deliveryAmountKopecks": self.delivery_amount_kopecks,
                "grandTotalKopecks": self.grand_total_kopecks,
                "weightGrams": self.total_weight_grams,
                "volumeMm3": self.total_volume_mm3,
                "cargoPlaces": self.cargo_places,
            },
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    window_start: int
    reset_at: int
    retry_after_seconds: int


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    event_type: str
    order_id: str
    status: str
    attempt_count: int
    available_at: int
    locked_until: Optional[int]
    lock_token: Optional[str]
    created_at: int


def _now(timestamp: Optional[int] = None) -> int:
    return int(time.time()) if timestamp is None else int(timestamp)


def _secret_bytes(secret: Union[str, bytes]) -> bytes:
    encoded = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if not encoded:
        raise ValueError("HMAC secret must not be empty")
    return encoded


def _digest(secret: bytes, purpose: str, value: str) -> str:
    message = purpose.encode("ascii") + b"\x00" + value.encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    if value > _MAX_BIGINT:
        raise ValueError(f"{field} exceeds database bigint range")
    return value


def _non_negative_integer(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    if value > _MAX_BIGINT:
        raise ValueError(f"{field} exceeds database bigint range")
    return value


class ProductRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _validate(product: Product) -> Product:
        sku = _required_text(product.sku, "sku")
        name = _required_text(product.name, "name")
        units_per_box = _positive_integer(product.units_per_box, "units_per_box")
        weight = _positive_integer(product.box_weight_grams, "box_weight_grams")
        volume = _positive_integer(product.box_volume_mm3, "box_volume_mm3")
        dimensions = (product.box_length_mm, product.box_width_mm, product.box_height_mm)
        if not all(value is not None for value in dimensions):
            raise ValueError("All three positive box dimensions are required")
        for field, value in zip(
            ("box_length_mm", "box_width_mm", "box_height_mm"), dimensions
        ):
            _positive_integer(value, field)  # type: ignore[arg-type]
        if volume != dimensions[0] * dimensions[1] * dimensions[2]:  # type: ignore[operator]
            raise ValueError("box_volume_mm3 must equal length_mm * width_mm * height_mm")
        if not isinstance(product.active, bool):
            raise ValueError("active must be a boolean")
        return replace(
            product,
            sku=sku,
            name=name,
            units_per_box=units_per_box,
            box_weight_grams=weight,
            box_volume_mm3=volume,
        )

    @staticmethod
    def _from_model(row: ProductModel) -> Product:
        return Product(
            sku=row.sku,
            name=row.name,
            units_per_box=row.units_per_box,
            box_weight_grams=row.box_weight_grams,
            box_volume_mm3=row.box_volume_mm3,
            box_length_mm=row.box_length_mm,
            box_width_mm=row.box_width_mm,
            box_height_mm=row.box_height_mm,
            active=row.active,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _values(product: Product, timestamp: int) -> dict[str, object]:
        return {
            "sku": product.sku,
            "name": product.name,
            "units_per_box": product.units_per_box,
            "box_weight_grams": product.box_weight_grams,
            "box_volume_mm3": product.box_volume_mm3,
            "box_length_mm": product.box_length_mm,
            "box_width_mm": product.box_width_mm,
            "box_height_mm": product.box_height_mm,
            "active": product.active,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def create(self, product: Product, *, now: Optional[int] = None) -> Product:
        product = self._validate(product)
        timestamp = _now(now)
        try:
            with self.database.transaction() as session:
                row = ProductModel(**self._values(product, timestamp))
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            raise ProductAlreadyExistsError(product.sku) from exc
        return self._from_model(row)

    def upsert(self, product: Product, *, now: Optional[int] = None) -> Product:
        product = self._validate(product)
        timestamp = _now(now)
        values = self._values(product, timestamp)
        with self.database.transaction() as session:
            if self.database.is_postgresql:
                statement = postgresql_insert(ProductModel).values(**values)
            else:
                statement = sqlite_insert(ProductModel).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["sku"],
                set_={
                    name: value
                    for name, value in values.items()
                    if name not in {"sku", "created_at"}
                },
            )
            session.execute(statement)
            row = session.get(ProductModel, product.sku)
            assert row is not None
        return self._from_model(row)

    def get(self, sku: str, *, active_only: bool = True) -> Optional[Product]:
        sku = _required_text(sku, "sku")
        statement = select(ProductModel).where(ProductModel.sku == sku)
        if active_only:
            statement = statement.where(ProductModel.active.is_(True))
        with self.database.session() as session:
            row = session.scalar(statement)
            return self._from_model(row) if row is not None else None

    fetch = get

    def fetch_many(
        self, skus: Iterable[str], *, active_only: bool = True
    ) -> Dict[str, Product]:
        cleaned = list(dict.fromkeys(_required_text(sku, "sku") for sku in skus))
        if not cleaned:
            return {}
        statement = select(ProductModel).where(ProductModel.sku.in_(cleaned))
        if active_only:
            statement = statement.where(ProductModel.active.is_(True))
        with self.database.session() as session:
            rows = session.scalars(statement).all()
            return {row.sku: self._from_model(row) for row in rows}

    def list(
        self, *, active_only: bool = True, limit: int = 1_000, offset: int = 0
    ) -> List[Product]:
        if limit <= 0 or limit > 10_000 or offset < 0:
            raise ValueError("Invalid pagination")
        statement = select(ProductModel)
        if active_only:
            statement = statement.where(ProductModel.active.is_(True))
        statement = statement.order_by(ProductModel.sku).limit(limit).offset(offset)
        with self.database.session() as session:
            return [self._from_model(row) for row in session.scalars(statement)]

    def count(self, *, active_only: bool = True) -> int:
        statement = select(func.count()).select_from(ProductModel)
        if active_only:
            statement = statement.where(ProductModel.active.is_(True))
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)

    def update(
        self, sku: str, *, now: Optional[int] = None, **changes: object
    ) -> Product:
        sku = _required_text(sku, "sku")
        unknown_fields = set(changes) - _PRODUCT_UPDATE_FIELDS
        if unknown_fields:
            raise ValueError("Unsupported product fields: " + ", ".join(unknown_fields))
        with self.database.transaction() as session:
            statement = select(ProductModel).where(ProductModel.sku == sku).with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise ProductNotFoundError(sku)
            current = self._from_model(row)
            if not changes:
                return current
            candidate = self._validate(replace(current, **changes))
            for name in _PRODUCT_UPDATE_FIELDS:
                setattr(row, name, getattr(candidate, name))
            row.updated_at = _now(now)
            session.flush()
            return self._from_model(row)

    def set_active(self, sku: str, active: bool, *, now: Optional[int] = None) -> Product:
        return self.update(sku, active=active, now=now)

    def delete(self, sku: str) -> bool:
        sku = _required_text(sku, "sku")
        with self.database.transaction() as session:
            result = session.execute(delete(ProductModel).where(ProductModel.sku == sku))
            return result.rowcount == 1

    def replace_price_tiers(
        self,
        sku: str,
        tiers: Sequence[ProductPriceTier],
        *,
        now: Optional[int] = None,
    ) -> tuple[ProductPriceTier, ...]:
        sku = _required_text(sku, "sku")
        normalized = tuple(tiers)
        if any(not isinstance(tier, DomainPriceTier) for tier in normalized):
            raise ValueError("tiers must contain ProductPriceTier values")
        timestamp = _now(now)
        with self.database.transaction() as session:
            product = session.scalar(
                select(ProductModel).where(ProductModel.sku == sku).with_for_update()
            )
            if product is None:
                raise ProductNotFoundError(sku)
            session.execute(
                delete(ProductPriceTierModel).where(
                    ProductPriceTierModel.product_sku == sku
                )
            )
            session.add_all(
                ProductPriceTierModel(
                    product_sku=sku,
                    min_boxes=tier.min_boxes,
                    max_boxes=tier.max_boxes,
                    price_per_unit_kopecks=tier.price_per_unit_kopecks,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                for tier in normalized
            )
        return normalized

    def fetch_catalog(self, skus: Iterable[str]) -> Dict[str, DomainProduct]:
        cleaned = list(dict.fromkeys(_required_text(sku, "sku") for sku in skus))
        if not cleaned:
            return {}
        with self.database.session() as session:
            products = session.scalars(
                select(ProductModel).where(
                    ProductModel.active.is_(True), ProductModel.sku.in_(cleaned)
                )
            ).all()
            tiers = session.scalars(
                select(ProductPriceTierModel)
                .where(ProductPriceTierModel.product_sku.in_(cleaned))
                .order_by(
                    ProductPriceTierModel.product_sku,
                    ProductPriceTierModel.min_boxes,
                    ProductPriceTierModel.max_boxes,
                )
            ).all()
        tiers_by_sku: Dict[str, list[DomainPriceTier]] = {sku: [] for sku in cleaned}
        for tier in tiers:
            tiers_by_sku[tier.product_sku].append(
                DomainPriceTier(
                    min_boxes=tier.min_boxes,
                    max_boxes=tier.max_boxes,
                    price_per_unit_kopecks=tier.price_per_unit_kopecks,
                )
            )
        return {
            row.sku: DomainProduct(
                sku=row.sku,
                name=row.name,
                units_per_box=row.units_per_box,
                weight_grams=row.box_weight_grams,
                length_mm=row.box_length_mm,
                width_mm=row.box_width_mm,
                height_mm=row.box_height_mm,
                price_tiers=tuple(tiers_by_sku[row.sku]),
            )
            for row in products
        }


class RateLimitRepository:
    """Persistent fixed-window counters safe across workers/processes."""

    def __init__(self, database: Database, hmac_secret: Union[str, bytes]) -> None:
        self.database = database
        self._secret = _secret_bytes(hmac_secret)

    def consume(
        self,
        subject: str,
        *,
        limit: int,
        window_seconds: int,
        scope: str = "orders",
        cost: int = 1,
        now: Optional[int] = None,
    ) -> RateLimitResult:
        subject = _required_text(subject, "subject")
        scope = _required_text(scope, "scope")
        limit = _positive_integer(limit, "limit")
        window_seconds = _positive_integer(window_seconds, "window_seconds")
        cost = _positive_integer(cost, "cost")
        timestamp = _now(now)
        window_start = timestamp - (timestamp % window_seconds)
        reset_at = window_start + window_seconds
        subject_digest = _digest(self._secret, f"rate-limit:{scope}", subject)
        values = {
            "scope": scope,
            "subject_digest": subject_digest,
            "window_start": window_start,
            "request_count": cost,
            "updated_at": timestamp,
        }
        with self.database.transaction() as session:
            session.execute(
                delete(RateLimitWindowModel).where(
                    RateLimitWindowModel.window_start < window_start - window_seconds
                )
            )
            if self.database.is_postgresql:
                statement = postgresql_insert(RateLimitWindowModel).values(**values)
            else:
                statement = sqlite_insert(RateLimitWindowModel).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["scope", "subject_digest", "window_start"],
                set_={
                    "request_count": RateLimitWindowModel.request_count + cost,
                    "updated_at": timestamp,
                },
            ).returning(RateLimitWindowModel.request_count)
            count = int(session.scalar(statement))
        allowed = count <= limit
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - count),
            window_start=window_start,
            reset_at=reset_at,
            retry_after_seconds=0 if allowed else max(0, reset_at - timestamp),
        )

    def prune(self, *, before: int) -> int:
        with self.database.transaction() as session:
            result = session.execute(
                delete(RateLimitWindowModel).where(
                    RateLimitWindowModel.window_start < int(before)
                )
            )
            return result.rowcount


class OrderRepository:
    """Create orders, trusted item snapshots and outbox events atomically."""

    def __init__(
        self,
        database: Database,
        hmac_secret: Union[str, bytes],
        *,
        idempotency_ttl_seconds: int = 86_400,
        duplicate_window_seconds: int = 300,
    ) -> None:
        self.database = database
        self._secret = _secret_bytes(hmac_secret)
        self.idempotency_ttl_seconds = _positive_integer(
            idempotency_ttl_seconds, "idempotency_ttl_seconds"
        )
        self.duplicate_window_seconds = _positive_integer(
            duplicate_window_seconds, "duplicate_window_seconds"
        )

    @staticmethod
    def _validated_items(items: Sequence[OrderItemInput]) -> List[OrderItemInput]:
        if not items:
            raise InvalidOrderError("Order must contain at least one item")
        validated: List[OrderItemInput] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, OrderItemInput):
                raise InvalidOrderError("items must contain OrderItemInput values")
            try:
                sku = _required_text(item.sku, "item.sku")
                boxes = _positive_integer(item.boxes, "item.boxes")
            except ValueError as exc:
                raise InvalidOrderError(str(exc)) from exc
            if sku in seen:
                raise InvalidOrderError("Order items must contain unique SKU values")
            seen.add(sku)
            validated.append(OrderItemInput(sku=sku, boxes=boxes))
        return validated

    @staticmethod
    def _validate_draft(draft: OrderDraft) -> List[OrderItemInput]:
        if not isinstance(draft, OrderDraft):
            raise InvalidOrderError("draft must be an OrderDraft")
        for field in (
            "buyer_type",
            "buyer_contact_name",
            "buyer_phone",
            "buyer_email",
            "delivery_method",
        ):
            try:
                _required_text(getattr(draft, field), field)
            except ValueError as exc:
                raise InvalidOrderError(str(exc)) from exc
        method = draft.delivery_method.strip()
        if method not in {"self_pickup", "cdek"}:
            raise InvalidOrderError("delivery_method must be self_pickup or cdek")
        if method == "self_pickup":
            if any(
                value is not None
                for value in (
                    draft.delivery_type,
                    draft.cdek_to_city_code,
                    draft.cdek_tariff_code,
                    draft.cdek_tariff_name,
                    draft.cdek_delivery_mode,
                    draft.cdek_period_min_days,
                    draft.cdek_period_max_days,
                    draft.delivery_region,
                    draft.delivery_city,
                    draft.delivery_office_code,
                    draft.delivery_postcode,
                    draft.delivery_street,
                    draft.delivery_house,
                    draft.delivery_apartment,
                )
            ):
                raise InvalidOrderError("self_pickup must not contain CDEK fields")
            if draft.delivery_amount_kopecks != 0:
                raise InvalidOrderError("self_pickup delivery amount must be zero")
        else:
            if draft.delivery_type not in {"pickup", "door"}:
                raise InvalidOrderError("CDEK delivery_type must be pickup or door")
            try:
                _positive_integer(draft.cdek_to_city_code, "cdek_to_city_code")
                _positive_integer(draft.cdek_tariff_code, "cdek_tariff_code")
                _required_text(draft.cdek_tariff_name, "cdek_tariff_name")
                delivery_mode = _positive_integer(
                    draft.cdek_delivery_mode, "cdek_delivery_mode"
                )
                if delivery_mode > 4:
                    raise ValueError("cdek_delivery_mode must be between 1 and 4")
                _non_negative_integer(
                    draft.delivery_amount_kopecks, "delivery_amount_kopecks"
                )
                period_min = _non_negative_integer(
                    draft.cdek_period_min_days, "cdek_period_min_days"
                )
                period_max = _non_negative_integer(
                    draft.cdek_period_max_days, "cdek_period_max_days"
                )
                if period_max < period_min:
                    raise ValueError("CDEK delivery period is invalid")
            except ValueError as exc:
                raise InvalidOrderError(str(exc)) from exc
            if draft.delivery_type == "pickup":
                try:
                    _required_text(
                        draft.delivery_office_code, "delivery_office_code"
                    )
                except ValueError as exc:
                    raise InvalidOrderError(str(exc)) from exc
                if any(
                    value is not None
                    for value in (
                        draft.delivery_postcode,
                        draft.delivery_street,
                        draft.delivery_house,
                        draft.delivery_apartment,
                    )
                ):
                    raise InvalidOrderError("CDEK pickup must not contain door address fields")
            else:
                for field in ("delivery_city", "delivery_street", "delivery_house"):
                    try:
                        _required_text(getattr(draft, field), field)
                    except ValueError as exc:
                        raise InvalidOrderError(str(exc)) from exc
                if draft.delivery_office_code is not None:
                    raise InvalidOrderError(
                        "CDEK door delivery must not contain delivery_office_code"
                    )
        return OrderRepository._validated_items(draft.items)

    @staticmethod
    def _items_for_order(session: Session, order_id: str) -> Tuple[OrderItemSnapshot, ...]:
        rows = session.scalars(
            select(OrderItemModel)
            .where(OrderItemModel.order_id == order_id)
            .order_by(OrderItemModel.line_number)
        ).all()
        return tuple(
            OrderItemSnapshot(
                sku=row.sku,
                product_name=row.product_name,
                boxes=row.boxes,
                units_per_box=row.units_per_box,
                units=row.units,
                price_per_unit_kopecks=row.price_per_unit_kopecks,
                price_per_box_kopecks=row.price_per_box_kopecks,
                line_amount_kopecks=row.line_amount_kopecks,
                unit_weight_grams=row.unit_weight_grams,
                unit_volume_mm3=row.unit_volume_mm3,
                box_length_mm=row.box_length_mm,
                box_width_mm=row.box_width_mm,
                box_height_mm=row.box_height_mm,
                total_weight_grams=row.total_weight_grams,
                total_volume_mm3=row.total_volume_mm3,
                cargo_places=row.cargo_places,
            )
            for row in rows
        )

    @classmethod
    def _response_for_order(
        cls, session: Session, order_id: str
    ) -> Optional[OrderResponse]:
        row = session.get(OrderModel, order_id)
        if row is None:
            return None
        return OrderResponse(
            order_id=row.id,
            status=row.status,
            created_at=row.created_at,
            total_boxes=row.total_boxes,
            total_units=row.total_units,
            products_amount_kopecks=row.products_amount_kopecks,
            delivery_amount_kopecks=row.delivery_amount_kopecks,
            grand_total_kopecks=row.grand_total_kopecks,
            total_weight_grams=row.total_weight_grams,
            total_volume_mm3=row.total_volume_mm3,
            cargo_places=row.cargo_places,
            delivery_method=row.delivery_method,
            delivery_type=row.delivery_type,
            cdek_to_city_code=row.cdek_to_city_code,
            cdek_tariff_code=row.cdek_tariff_code,
            cdek_tariff_name=row.cdek_tariff_name,
            cdek_delivery_mode=row.cdek_delivery_mode,
            cdek_period_min_days=row.cdek_period_min_days,
            cdek_period_max_days=row.cdek_period_max_days,
            delivery_region=row.delivery_region,
            delivery_city=row.delivery_city,
            delivery_office_code=row.delivery_office_code,
            delivery_postcode=row.delivery_postcode,
            delivery_street=row.delivery_street,
            delivery_house=row.delivery_house,
            delivery_apartment=row.delivery_apartment,
            items=cls._items_for_order(session, order_id),
        )

    @staticmethod
    def _advisory_lock_id(digest: str) -> int:
        value = int(digest[:16], 16)
        return value - (1 << 64) if value >= (1 << 63) else value

    def _lock_digest(self, session: Session, digest: str) -> None:
        if self.database.is_postgresql:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": self._advisory_lock_id(digest)},
            )

    def lookup_idempotency(
        self, *, idempotency_key: str, request_hash: str, now: Optional[int] = None
    ) -> Optional[OrderResponse]:
        clean_key = _required_text(idempotency_key, "idempotency_key")
        clean_request_hash = _required_text(request_hash, "request_hash")
        if len(clean_key) > 512:
            raise InvalidOrderError("idempotency_key is too long")
        timestamp = _now(now)
        key_digest = _digest(self._secret, "idempotency-key", clean_key)
        request_digest = _digest(self._secret, "idempotency-request", clean_request_hash)
        with self.database.transaction() as session:
            session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.expires_at <= timestamp
                )
            )
            existing = session.get(IdempotencyRecordModel, key_digest)
            if existing is None:
                return None
            if not hmac.compare_digest(existing.request_digest, request_digest):
                raise IdempotencyConflictError(
                    "Idempotency key was already used for another request"
                )
            response = self._response_for_order(session, existing.order_id)
            if response is None:
                raise RepositoryError("Idempotency record references no order")
            return response

    def create_order(
        self,
        draft: OrderDraft,
        *,
        calculation: OrderCalculation,
        request_hash: str,
        duplicate_fingerprint: str,
        idempotency_key: Optional[str] = None,
        outbox_event_type: str = "order.created",
        enqueue_outbox: bool = True,
        now: Optional[int] = None,
    ) -> CreateOrderResult:
        items = self._validate_draft(draft)
        if not isinstance(calculation, OrderCalculation):
            raise InvalidOrderError("calculation must be an OrderCalculation")
        if [(item.sku, item.boxes) for item in items] != [
            (item.sku, item.boxes) for item in calculation.items
        ]:
            raise InvalidOrderError("calculation does not match draft items")
        snapshots = tuple(
            OrderItemSnapshot(
                sku=item.sku,
                product_name=item.name,
                boxes=item.boxes,
                units_per_box=item.units_per_box,
                units=item.units,
                price_per_unit_kopecks=item.price_per_unit_kopecks,
                price_per_box_kopecks=item.price_per_box_kopecks,
                line_amount_kopecks=item.line_amount_kopecks,
                unit_weight_grams=item.weight_per_box_grams,
                unit_volume_mm3=item.box_volume_mm3,
                box_length_mm=item.length_mm,
                box_width_mm=item.width_mm,
                box_height_mm=item.height_mm,
                total_weight_grams=item.total_weight_grams,
                total_volume_mm3=item.total_volume_mm3,
                cargo_places=item.cargo_places,
            )
            for item in calculation.items
        )
        totals = calculation.totals
        try:
            delivery_amount = _non_negative_integer(
                draft.delivery_amount_kopecks, "delivery_amount_kopecks"
            )
            grand_total = _positive_integer(
                totals.products_amount_kopecks + delivery_amount,
                "grand_total_kopecks",
            )
        except ValueError as exc:
            raise InvalidOrderError(str(exc)) from exc
        try:
            request_hash = _required_text(request_hash, "request_hash")
            duplicate_fingerprint = _required_text(
                duplicate_fingerprint, "duplicate_fingerprint"
            )
            event_type = _required_text(outbox_event_type, "outbox_event_type")
        except ValueError as exc:
            raise InvalidOrderError(str(exc)) from exc
        timestamp = _now(now)
        request_digest = _digest(self._secret, "idempotency-request", request_hash)
        fingerprint_digest = _digest(
            self._secret, "duplicate-fingerprint", duplicate_fingerprint
        )
        key_digest: Optional[str] = None
        if idempotency_key is not None:
            try:
                clean_key = _required_text(idempotency_key, "idempotency_key")
            except ValueError as exc:
                raise InvalidOrderError(str(exc)) from exc
            if len(clean_key) > 512:
                raise InvalidOrderError("idempotency_key is too long")
            key_digest = _digest(self._secret, "idempotency-key", clean_key)

        with self.database.transaction() as session:
            session.execute(
                delete(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.expires_at <= timestamp
                )
            )
            if key_digest is not None:
                self._lock_digest(session, key_digest)
                existing = session.get(IdempotencyRecordModel, key_digest)
                if existing is not None:
                    if not hmac.compare_digest(existing.request_digest, request_digest):
                        raise IdempotencyConflictError(
                            "Idempotency key was already used for another request"
                        )
                    response = self._response_for_order(session, existing.order_id)
                    if response is None:
                        raise RepositoryError("Idempotency record references no order")
                    return CreateOrderResult(response, False, True, False)

            self._lock_digest(session, fingerprint_digest)
            duplicate_id = session.scalar(
                select(OrderModel.id)
                .where(
                    OrderModel.request_fingerprint_digest == fingerprint_digest,
                    OrderModel.created_at >= timestamp - self.duplicate_window_seconds,
                )
                .order_by(OrderModel.created_at.desc())
                .limit(1)
            )
            if duplicate_id is not None:
                response = self._response_for_order(session, duplicate_id)
                if response is None:
                    raise RepositoryError("Duplicate query returned no order")
                return CreateOrderResult(response, False, False, True)

            order_id = str(uuid.uuid4())
            session.add(
                OrderModel(
                    id=order_id,
                    status="accepted",
                    buyer_type=draft.buyer_type.strip(),
                    buyer_contact_name=draft.buyer_contact_name.strip(),
                    buyer_phone=draft.buyer_phone.strip(),
                    buyer_email=draft.buyer_email.strip(),
                    company_name=draft.company_name,
                    company_inn=draft.company_inn,
                    company_kpp=draft.company_kpp,
                    company_legal_address=draft.company_legal_address,
                    delivery_method=draft.delivery_method.strip(),
                    delivery_type=draft.delivery_type,
                    cdek_to_city_code=draft.cdek_to_city_code,
                    cdek_tariff_code=draft.cdek_tariff_code,
                    cdek_tariff_name=draft.cdek_tariff_name,
                    cdek_delivery_mode=draft.cdek_delivery_mode,
                    cdek_period_min_days=draft.cdek_period_min_days,
                    cdek_period_max_days=draft.cdek_period_max_days,
                    delivery_region=draft.delivery_region,
                    delivery_city=draft.delivery_city,
                    delivery_office_code=draft.delivery_office_code,
                    delivery_postcode=draft.delivery_postcode,
                    delivery_street=draft.delivery_street,
                    delivery_house=draft.delivery_house,
                    delivery_apartment=draft.delivery_apartment,
                    recipient_contact_name=draft.recipient_contact_name,
                    recipient_phone=draft.recipient_phone,
                    recipient_email=draft.recipient_email,
                    comment=draft.comment,
                    total_boxes=totals.total_boxes,
                    total_units=totals.total_units,
                    products_amount_kopecks=totals.products_amount_kopecks,
                    delivery_amount_kopecks=delivery_amount,
                    grand_total_kopecks=grand_total,
                    total_weight_grams=totals.total_weight_grams,
                    total_volume_mm3=totals.total_volume_mm3,
                    cargo_places=totals.cargo_places,
                    request_fingerprint_digest=fingerprint_digest,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            # No ORM relationships are needed by the repository, so flush the
            # parent explicitly before inserting FK-dependent snapshots/events.
            session.flush()
            session.add_all(
                OrderItemModel(
                    order_id=order_id,
                    line_number=line_number,
                    sku=snapshot.sku,
                    product_name=snapshot.product_name,
                    boxes=snapshot.boxes,
                    units_per_box=snapshot.units_per_box,
                    units=snapshot.units,
                    price_per_unit_kopecks=snapshot.price_per_unit_kopecks,
                    price_per_box_kopecks=snapshot.price_per_box_kopecks,
                    line_amount_kopecks=snapshot.line_amount_kopecks,
                    unit_weight_grams=snapshot.unit_weight_grams,
                    unit_volume_mm3=snapshot.unit_volume_mm3,
                    box_length_mm=snapshot.box_length_mm,
                    box_width_mm=snapshot.box_width_mm,
                    box_height_mm=snapshot.box_height_mm,
                    total_weight_grams=snapshot.total_weight_grams,
                    total_volume_mm3=snapshot.total_volume_mm3,
                    cargo_places=snapshot.cargo_places,
                )
                for line_number, snapshot in enumerate(snapshots, start=1)
            )
            if enqueue_outbox:
                session.add(
                    OutboxModel(
                        event_type=event_type,
                        order_id=order_id,
                        status="pending",
                        attempt_count=0,
                        available_at=timestamp,
                        locked_until=None,
                        lock_token=None,
                        last_error=None,
                        created_at=timestamp,
                        updated_at=timestamp,
                        succeeded_at=None,
                    )
                )
            if key_digest is not None:
                session.add(
                    IdempotencyRecordModel(
                        key_digest=key_digest,
                        request_digest=request_digest,
                        order_id=order_id,
                        created_at=timestamp,
                        expires_at=timestamp + self.idempotency_ttl_seconds,
                    )
                )
            session.flush()
            response = OrderResponse(
                order_id=order_id,
                status="accepted",
                created_at=timestamp,
                total_boxes=totals.total_boxes,
                total_units=totals.total_units,
                products_amount_kopecks=totals.products_amount_kopecks,
                delivery_amount_kopecks=delivery_amount,
                grand_total_kopecks=grand_total,
                total_weight_grams=totals.total_weight_grams,
                total_volume_mm3=totals.total_volume_mm3,
                cargo_places=totals.cargo_places,
                delivery_method=draft.delivery_method.strip(),
                delivery_type=draft.delivery_type,
                cdek_to_city_code=draft.cdek_to_city_code,
                cdek_tariff_code=draft.cdek_tariff_code,
                cdek_tariff_name=draft.cdek_tariff_name,
                cdek_delivery_mode=draft.cdek_delivery_mode,
                cdek_period_min_days=draft.cdek_period_min_days,
                cdek_period_max_days=draft.cdek_period_max_days,
                delivery_region=draft.delivery_region,
                delivery_city=draft.delivery_city,
                delivery_office_code=draft.delivery_office_code,
                delivery_postcode=draft.delivery_postcode,
                delivery_street=draft.delivery_street,
                delivery_house=draft.delivery_house,
                delivery_apartment=draft.delivery_apartment,
                items=snapshots,
            )
            return CreateOrderResult(response, True, False, False)

    def get_order_response(self, order_id: str) -> Optional[OrderResponse]:
        order_id = _required_text(order_id, "order_id")
        with self.database.session() as session:
            return self._response_for_order(session, order_id)

    def get_order_for_webhook(self, order_id: str) -> Optional[WebhookOrder]:
        order_id = _required_text(order_id, "order_id")
        with self.database.session() as session:
            row = session.get(OrderModel, order_id)
            if row is None:
                return None
            return WebhookOrder(
                order_id=row.id,
                status=row.status,
                created_at=row.created_at,
                buyer_type=row.buyer_type,
                buyer_contact_name=row.buyer_contact_name,
                buyer_phone=row.buyer_phone,
                buyer_email=row.buyer_email,
                company_name=row.company_name,
                company_inn=row.company_inn,
                company_kpp=row.company_kpp,
                company_legal_address=row.company_legal_address,
                delivery_method=row.delivery_method,
                delivery_type=row.delivery_type,
                cdek_to_city_code=row.cdek_to_city_code,
                cdek_tariff_code=row.cdek_tariff_code,
                cdek_tariff_name=row.cdek_tariff_name,
                cdek_delivery_mode=row.cdek_delivery_mode,
                cdek_period_min_days=row.cdek_period_min_days,
                cdek_period_max_days=row.cdek_period_max_days,
                delivery_region=row.delivery_region,
                delivery_city=row.delivery_city,
                delivery_office_code=row.delivery_office_code,
                delivery_postcode=row.delivery_postcode,
                delivery_street=row.delivery_street,
                delivery_house=row.delivery_house,
                delivery_apartment=row.delivery_apartment,
                recipient_contact_name=row.recipient_contact_name,
                recipient_phone=row.recipient_phone,
                recipient_email=row.recipient_email,
                comment=row.comment,
                total_boxes=row.total_boxes,
                total_units=row.total_units,
                products_amount_kopecks=row.products_amount_kopecks,
                delivery_amount_kopecks=row.delivery_amount_kopecks,
                grand_total_kopecks=row.grand_total_kopecks,
                total_weight_grams=row.total_weight_grams,
                total_volume_mm3=row.total_volume_mm3,
                cargo_places=row.cargo_places,
                items=self._items_for_order(session, order_id),
            )


class OutboxRepository:
    """Durable outbox using row locks with SKIP LOCKED on PostgreSQL."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_model(row: OutboxModel) -> OutboxMessage:
        return OutboxMessage(
            id=row.id,
            event_type=row.event_type,
            order_id=row.order_id,
            status=row.status,
            attempt_count=row.attempt_count,
            available_at=row.available_at,
            locked_until=row.locked_until,
            lock_token=row.lock_token,
            created_at=row.created_at,
        )

    @staticmethod
    def _safe_error(error: object) -> str:
        value = str(error).replace("\r", " ").replace("\n", " ").replace("\x00", " ")
        return value[:1_000]

    def claim(
        self,
        *,
        limit: int = 10,
        lease_seconds: int = 60,
        max_attempts: int = 10,
        now: Optional[int] = None,
    ) -> List[OutboxMessage]:
        limit = _positive_integer(limit, "limit")
        lease_seconds = _positive_integer(lease_seconds, "lease_seconds")
        max_attempts = _positive_integer(max_attempts, "max_attempts")
        if limit > 1_000:
            raise ValueError("limit must not exceed 1000")
        timestamp = _now(now)
        claimed: List[OutboxMessage] = []
        available = or_(
            (OutboxModel.status == "pending") & (OutboxModel.available_at <= timestamp),
            (OutboxModel.status == "processing")
            & (OutboxModel.locked_until <= timestamp),
        )
        with self.database.transaction() as session:
            session.execute(
                update(OutboxModel)
                .where(OutboxModel.attempt_count >= max_attempts, available)
                .values(
                    status="failed",
                    locked_until=None,
                    lock_token=None,
                    last_error="Maximum delivery attempts exceeded",
                    updated_at=timestamp,
                )
            )
            candidates = session.scalars(
                select(OutboxModel)
                .where(OutboxModel.attempt_count < max_attempts, available)
                .order_by(OutboxModel.available_at, OutboxModel.id)
                .limit(limit)
                .with_for_update(skip_locked=self.database.is_postgresql)
            ).all()
            for row in candidates:
                row.status = "processing"
                row.attempt_count += 1
                row.locked_until = timestamp + lease_seconds
                row.lock_token = uuid.uuid4().hex
                row.updated_at = timestamp
                claimed.append(self._from_model(row))
            session.flush()
        return claimed

    def mark_success(
        self, message_id: int, lock_token: str, *, now: Optional[int] = None
    ) -> bool:
        timestamp = _now(now)
        with self.database.transaction() as session:
            result = session.execute(
                update(OutboxModel)
                .where(
                    OutboxModel.id == int(message_id),
                    OutboxModel.status == "processing",
                    OutboxModel.lock_token == lock_token,
                )
                .values(
                    status="succeeded",
                    locked_until=None,
                    lock_token=None,
                    last_error=None,
                    succeeded_at=timestamp,
                    updated_at=timestamp,
                )
            )
            return result.rowcount == 1

    success = mark_success

    def mark_retry(
        self,
        message_id: int,
        lock_token: str,
        error: object,
        *,
        delay_seconds: int,
        now: Optional[int] = None,
    ) -> bool:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        timestamp = _now(now)
        with self.database.transaction() as session:
            result = session.execute(
                update(OutboxModel)
                .where(
                    OutboxModel.id == int(message_id),
                    OutboxModel.status == "processing",
                    OutboxModel.lock_token == lock_token,
                )
                .values(
                    status="pending",
                    available_at=timestamp + int(delay_seconds),
                    locked_until=None,
                    lock_token=None,
                    last_error=self._safe_error(error),
                    updated_at=timestamp,
                )
            )
            return result.rowcount == 1

    retry = mark_retry

    def mark_failed(
        self,
        message_id: int,
        lock_token: str,
        error: object,
        *,
        now: Optional[int] = None,
    ) -> bool:
        timestamp = _now(now)
        with self.database.transaction() as session:
            result = session.execute(
                update(OutboxModel)
                .where(
                    OutboxModel.id == int(message_id),
                    OutboxModel.status == "processing",
                    OutboxModel.lock_token == lock_token,
                )
                .values(
                    status="failed",
                    locked_until=None,
                    lock_token=None,
                    last_error=self._safe_error(error),
                    updated_at=timestamp,
                )
            )
            return result.rowcount == 1

    fail = mark_failed

    def get(self, message_id: int) -> Optional[OutboxMessage]:
        with self.database.session() as session:
            row = session.get(OutboxModel, int(message_id))
            return self._from_model(row) if row is not None else None

    def get_for_order(self, order_id: str) -> Optional[OutboxMessage]:
        order_id = _required_text(order_id, "order_id")
        with self.database.session() as session:
            row = session.scalar(
                select(OutboxModel)
                .where(OutboxModel.order_id == order_id)
                .order_by(OutboxModel.id.desc())
                .limit(1)
            )
            return self._from_model(row) if row is not None else None

    def count(self, *, status: Optional[str] = None) -> int:
        statement = select(func.count()).select_from(OutboxModel)
        if status is not None:
            statement = statement.where(OutboxModel.status == status)
        with self.database.session() as session:
            return int(session.scalar(statement) or 0)


__all__ = [
    "CreateOrderResult",
    "IdempotencyConflictError",
    "InvalidOrderError",
    "OrderDraft",
    "OrderItemInput",
    "OrderItemSnapshot",
    "OrderRepository",
    "OrderResponse",
    "OutboxMessage",
    "OutboxRepository",
    "Product",
    "ProductAlreadyExistsError",
    "ProductCatalogError",
    "ProductNotFoundError",
    "ProductPriceTier",
    "ProductRepository",
    "RateLimitRepository",
    "RateLimitResult",
    "RepositoryError",
    "UnknownProductError",
    "WebhookOrder",
]
