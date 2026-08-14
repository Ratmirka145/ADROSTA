"""Persistence repositories for products, orders, limits and delivery events.

Only trusted product snapshots and normalized order fields are stored.  Raw
idempotency keys, client/IP identifiers and request fingerprints never reach
SQLite: they are domain-separated HMAC-SHA256 digests.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from app.database import Database
from app.domain import (
    OrderCalculation,
    PriceTier as DomainPriceTier,
    Product as DomainProduct,
)


_SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807
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
    total_weight_grams: int
    total_volume_mm3: int
    cargo_places: int
    items: Tuple[OrderItemSnapshot, ...]

    def as_dict(self) -> dict:
        """Return the PII-free object safe to send to the browser."""

        return {
            "orderId": self.order_id,
            "status": self.status,
            "createdAt": self.created_at,
            "totals": {
                "boxes": self.total_boxes,
                "totalUnits": self.total_units,
                "productsAmountKopecks": self.products_amount_kopecks,
                "weightGrams": self.total_weight_grams,
                "volumeMm3": self.total_volume_mm3,
                "cargoPlaces": self.cargo_places,
            },
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
    total_weight_grams: int
    total_volume_mm3: int
    cargo_places: int
    items: Tuple[OrderItemSnapshot, ...]

    def as_dict(self) -> dict:
        """Reconstruct the nested contract only when dispatching a webhook."""

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
            ("region", self.delivery_region),
            ("city", self.delivery_city),
            ("officeCode", self.delivery_office_code),
            ("postcode", self.delivery_postcode),
            ("street", self.delivery_street),
            ("house", self.delivery_house),
            ("apartment", self.delivery_apartment),
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
    if value > _SQLITE_MAX_INTEGER:
        raise ValueError(f"{field} exceeds SQLite integer range")
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

        dimensions = (
            product.box_length_mm,
            product.box_width_mm,
            product.box_height_mm,
        )
        if not all(value is not None for value in dimensions):
            raise ValueError("All three positive box dimensions are required")
        for field, value in zip(
            ("box_length_mm", "box_width_mm", "box_height_mm"), dimensions
        ):
            _positive_integer(value, field)  # type: ignore[arg-type]
        calculated_volume = dimensions[0] * dimensions[1] * dimensions[2]  # type: ignore[operator]
        if volume != calculated_volume:
            raise ValueError(
                "box_volume_mm3 must equal length_mm * width_mm * height_mm"
            )

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
    def _from_row(row: sqlite3.Row) -> Product:
        return Product(
            sku=row["sku"],
            name=row["name"],
            units_per_box=row["units_per_box"],
            box_weight_grams=row["box_weight_grams"],
            box_volume_mm3=row["box_volume_mm3"],
            box_length_mm=row["box_length_mm"],
            box_width_mm=row["box_width_mm"],
            box_height_mm=row["box_height_mm"],
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, product: Product, *, now: Optional[int] = None) -> Product:
        product = self._validate(product)
        timestamp = _now(now)
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO products (
                        sku, name, units_per_box, box_weight_grams, box_volume_mm3,
                        box_length_mm, box_width_mm, box_height_mm,
                        active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product.sku,
                        product.name,
                        product.units_per_box,
                        product.box_weight_grams,
                        product.box_volume_mm3,
                        product.box_length_mm,
                        product.box_width_mm,
                        product.box_height_mm,
                        int(product.active),
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "products.sku" in str(exc):
                raise ProductAlreadyExistsError(product.sku) from exc
            raise
        created = self.get(product.sku, active_only=False)
        assert created is not None
        return created

    def upsert(self, product: Product, *, now: Optional[int] = None) -> Product:
        product = self._validate(product)
        timestamp = _now(now)
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO products (
                    sku, name, units_per_box, box_weight_grams, box_volume_mm3,
                    box_length_mm, box_width_mm, box_height_mm,
                    active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    name = excluded.name,
                    units_per_box = excluded.units_per_box,
                    box_weight_grams = excluded.box_weight_grams,
                    box_volume_mm3 = excluded.box_volume_mm3,
                    box_length_mm = excluded.box_length_mm,
                    box_width_mm = excluded.box_width_mm,
                    box_height_mm = excluded.box_height_mm,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (
                    product.sku,
                    product.name,
                    product.units_per_box,
                    product.box_weight_grams,
                    product.box_volume_mm3,
                    product.box_length_mm,
                    product.box_width_mm,
                    product.box_height_mm,
                    int(product.active),
                    timestamp,
                    timestamp,
                ),
            )
        saved = self.get(product.sku, active_only=False)
        assert saved is not None
        return saved

    def get(self, sku: str, *, active_only: bool = True) -> Optional[Product]:
        sku = _required_text(sku, "sku")
        query = "SELECT * FROM products WHERE sku = ?"
        parameters: List[object] = [sku]
        if active_only:
            query += " AND active = 1"
        with self.database.connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._from_row(row) if row is not None else None

    fetch = get

    def fetch_many(
        self, skus: Iterable[str], *, active_only: bool = True
    ) -> Dict[str, Product]:
        cleaned = list(dict.fromkeys(_required_text(sku, "sku") for sku in skus))
        if not cleaned:
            return {}

        found: Dict[str, Product] = {}
        with self.database.connection() as connection:
            for offset in range(0, len(cleaned), 500):
                chunk = cleaned[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                query = f"SELECT * FROM products WHERE sku IN ({placeholders})"
                if active_only:
                    query += " AND active = 1"
                for row in connection.execute(query, chunk).fetchall():
                    product = self._from_row(row)
                    found[product.sku] = product
        return found

    def list(
        self,
        *,
        active_only: bool = True,
        limit: int = 1_000,
        offset: int = 0,
    ) -> List[Product]:
        if limit <= 0 or limit > 10_000 or offset < 0:
            raise ValueError("Invalid pagination")
        query = "SELECT * FROM products"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY sku LIMIT ? OFFSET ?"
        with self.database.connection() as connection:
            rows = connection.execute(query, (limit, offset)).fetchall()
        return [self._from_row(row) for row in rows]

    def count(self, *, active_only: bool = True) -> int:
        query = "SELECT COUNT(*) FROM products"
        if active_only:
            query += " WHERE active = 1"
        with self.database.connection() as connection:
            return int(connection.execute(query).fetchone()[0])

    def update(
        self, sku: str, *, now: Optional[int] = None, **changes: object
    ) -> Product:
        sku = _required_text(sku, "sku")
        unknown_fields = set(changes) - _PRODUCT_UPDATE_FIELDS
        if unknown_fields:
            raise ValueError("Unsupported product fields: " + ", ".join(unknown_fields))
        current = self.get(sku, active_only=False)
        if current is None:
            raise ProductNotFoundError(sku)
        if not changes:
            return current

        candidate = self._validate(replace(current, **changes))
        timestamp = _now(now)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE products SET
                    name = ?, units_per_box = ?, box_weight_grams = ?, box_volume_mm3 = ?,
                    box_length_mm = ?, box_width_mm = ?, box_height_mm = ?,
                    active = ?, updated_at = ?
                WHERE sku = ?
                """,
                (
                    candidate.name,
                    candidate.units_per_box,
                    candidate.box_weight_grams,
                    candidate.box_volume_mm3,
                    candidate.box_length_mm,
                    candidate.box_width_mm,
                    candidate.box_height_mm,
                    int(candidate.active),
                    timestamp,
                    sku,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductNotFoundError(sku)
        updated = self.get(sku, active_only=False)
        assert updated is not None
        return updated

    def set_active(
        self, sku: str, active: bool, *, now: Optional[int] = None
    ) -> Product:
        return self.update(sku, active=active, now=now)

    def delete(self, sku: str) -> bool:
        sku = _required_text(sku, "sku")
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM products WHERE sku = ?", (sku,))
            return cursor.rowcount == 1

    def replace_price_tiers(
        self,
        sku: str,
        tiers: Sequence[ProductPriceTier],
        *,
        now: Optional[int] = None,
    ) -> tuple[ProductPriceTier, ...]:
        """Atomically replace persisted tiers without applying pricing rules."""

        sku = _required_text(sku, "sku")
        normalized = tuple(tiers)
        if any(not isinstance(tier, DomainPriceTier) for tier in normalized):
            raise ValueError("tiers must contain ProductPriceTier values")
        timestamp = _now(now)
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM products WHERE sku = ?", (sku,)
            ).fetchone()
            if exists is None:
                raise ProductNotFoundError(sku)
            connection.execute(
                "DELETE FROM product_price_tiers WHERE product_sku = ?", (sku,)
            )
            connection.executemany(
                """
                INSERT INTO product_price_tiers (
                    product_sku, min_boxes, max_boxes,
                    price_per_unit_kopecks, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        sku,
                        tier.min_boxes,
                        tier.max_boxes,
                        tier.price_per_unit_kopecks,
                        timestamp,
                        timestamp,
                    )
                    for tier in normalized
                ),
            )
        return normalized

    def fetch_catalog(self, skus: Iterable[str]) -> Dict[str, DomainProduct]:
        """Load canonical products and all their SKU-specific price tiers."""

        cleaned = list(dict.fromkeys(_required_text(sku, "sku") for sku in skus))
        if not cleaned:
            return {}

        rows_by_sku: Dict[str, sqlite3.Row] = {}
        tiers_by_sku: Dict[str, list[DomainPriceTier]] = {
            sku: [] for sku in cleaned
        }
        with self.database.connection() as connection:
            for offset in range(0, len(cleaned), 500):
                chunk = cleaned[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                product_rows = connection.execute(
                    f"SELECT * FROM products "
                    f"WHERE active = 1 AND sku IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in product_rows:
                    rows_by_sku[row["sku"]] = row
                tier_rows = connection.execute(
                    f"""
                    SELECT product_sku, min_boxes, max_boxes,
                           price_per_unit_kopecks
                    FROM product_price_tiers
                    WHERE product_sku IN ({placeholders})
                    ORDER BY product_sku, min_boxes, max_boxes
                    """,
                    chunk,
                ).fetchall()
                for row in tier_rows:
                    tiers_by_sku[row["product_sku"]].append(
                        DomainPriceTier(
                            min_boxes=row["min_boxes"],
                            max_boxes=row["max_boxes"],
                            price_per_unit_kopecks=row[
                                "price_per_unit_kopecks"
                            ],
                        )
                    )

        return {
            sku: DomainProduct(
                sku=sku,
                name=row["name"],
                units_per_box=row["units_per_box"],
                weight_grams=row["box_weight_grams"],
                length_mm=row["box_length_mm"],
                width_mm=row["box_width_mm"],
                height_mm=row["box_height_mm"],
                price_tiers=tuple(tiers_by_sku[sku]),
            )
            for sku, row in rows_by_sku.items()
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

        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM rate_limit_windows WHERE window_start < ?",
                (window_start - window_seconds,),
            )
            connection.execute(
                """
                INSERT INTO rate_limit_windows (
                    scope, subject_digest, window_start, request_count, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, subject_digest, window_start) DO UPDATE SET
                    request_count = request_count + excluded.request_count,
                    updated_at = excluded.updated_at
                """,
                (scope, subject_digest, window_start, cost, timestamp),
            )
            count = int(
                connection.execute(
                    """
                    SELECT request_count FROM rate_limit_windows
                    WHERE scope = ? AND subject_digest = ? AND window_start = ?
                    """,
                    (scope, subject_digest, window_start),
                ).fetchone()[0]
            )

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
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM rate_limit_windows WHERE window_start < ?", (int(before),)
            )
            return cursor.rowcount


class OrderRepository:
    """Creates orders, trusted item snapshots and outbox events atomically."""

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
        else:
            if draft.delivery_type not in {"pickup", "door"}:
                raise InvalidOrderError("CDEK delivery_type must be pickup or door")
            try:
                _required_text(draft.delivery_city, "delivery_city")
            except ValueError as exc:
                raise InvalidOrderError(str(exc)) from exc
            if draft.delivery_type == "pickup":
                if any(
                    value is not None
                    for value in (
                        draft.delivery_postcode,
                        draft.delivery_street,
                        draft.delivery_house,
                        draft.delivery_apartment,
                    )
                ):
                    raise InvalidOrderError(
                        "CDEK pickup must not contain door address fields"
                    )
            else:
                for field in ("delivery_street", "delivery_house"):
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
    def _items_for_order(
        connection: sqlite3.Connection, order_id: str
    ) -> Tuple[OrderItemSnapshot, ...]:
        rows = connection.execute(
            """
            SELECT * FROM order_items
            WHERE order_id = ? ORDER BY line_number
            """,
            (order_id,),
        ).fetchall()
        return tuple(
            OrderItemSnapshot(
                sku=row["sku"],
                product_name=row["product_name"],
                boxes=row["boxes"],
                units_per_box=row["units_per_box"],
                units=row["units"],
                price_per_unit_kopecks=row["price_per_unit_kopecks"],
                price_per_box_kopecks=row["price_per_box_kopecks"],
                line_amount_kopecks=row["line_amount_kopecks"],
                unit_weight_grams=row["unit_weight_grams"],
                unit_volume_mm3=row["unit_volume_mm3"],
                box_length_mm=row["box_length_mm"],
                box_width_mm=row["box_width_mm"],
                box_height_mm=row["box_height_mm"],
                total_weight_grams=row["total_weight_grams"],
                total_volume_mm3=row["total_volume_mm3"],
                cargo_places=row["cargo_places"],
            )
            for row in rows
        )

    @classmethod
    def _response_for_order(
        cls, connection: sqlite3.Connection, order_id: str
    ) -> Optional[OrderResponse]:
        row = connection.execute(
            """
            SELECT id, status, created_at, total_boxes, total_units,
                   products_amount_kopecks, total_weight_grams,
                   total_volume_mm3, cargo_places
            FROM orders WHERE id = ?
            """,
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        return OrderResponse(
            order_id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            total_boxes=row["total_boxes"],
            total_units=row["total_units"],
            products_amount_kopecks=row["products_amount_kopecks"],
            total_weight_grams=row["total_weight_grams"],
            total_volume_mm3=row["total_volume_mm3"],
            cargo_places=row["cargo_places"],
            items=cls._items_for_order(connection, order_id),
        )

    def lookup_idempotency(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        now: Optional[int] = None,
    ) -> Optional[OrderResponse]:
        clean_key = _required_text(idempotency_key, "idempotency_key")
        clean_request_hash = _required_text(request_hash, "request_hash")
        if len(clean_key) > 512:
            raise InvalidOrderError("idempotency_key is too long")
        timestamp = _now(now)
        key_digest = _digest(self._secret, "idempotency-key", clean_key)
        request_digest = _digest(
            self._secret,
            "idempotency-request",
            clean_request_hash,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at <= ?",
                (timestamp,),
            )
            existing = connection.execute(
                """
                SELECT request_digest, order_id FROM idempotency_records
                WHERE key_digest = ?
                """,
                (key_digest,),
            ).fetchone()
            if existing is None:
                return None
            if not hmac.compare_digest(existing["request_digest"], request_digest):
                raise IdempotencyConflictError(
                    "Idempotency key was already used for another request"
                )
            response = self._response_for_order(connection, existing["order_id"])
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

        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at <= ?", (timestamp,)
            )

            if key_digest is not None:
                existing = connection.execute(
                    """
                    SELECT request_digest, order_id FROM idempotency_records
                    WHERE key_digest = ?
                    """,
                    (key_digest,),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(existing["request_digest"], request_digest):
                        raise IdempotencyConflictError(
                            "Idempotency key was already used for another request"
                        )
                    response = self._response_for_order(
                        connection, existing["order_id"]
                    )
                    if response is None:
                        raise RepositoryError("Idempotency record references no order")
                    return CreateOrderResult(
                        response=response,
                        created=False,
                        replayed=True,
                        duplicate=False,
                    )

            duplicate = connection.execute(
                """
                SELECT id FROM orders
                WHERE request_fingerprint_digest = ? AND created_at >= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint_digest, timestamp - self.duplicate_window_seconds),
            ).fetchone()
            if duplicate is not None:
                response = self._response_for_order(connection, duplicate["id"])
                if response is None:
                    raise RepositoryError("Duplicate query returned no order")
                return CreateOrderResult(
                    response=response,
                    created=False,
                    replayed=False,
                    duplicate=True,
                )

            order_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO orders (
                    id, status, buyer_type, buyer_contact_name, buyer_phone,
                    buyer_email, company_name, company_inn, company_kpp,
                    company_legal_address, delivery_method, delivery_type,
                    delivery_region, delivery_city, delivery_office_code,
                    delivery_postcode, delivery_street, delivery_house,
                    delivery_apartment, recipient_contact_name, recipient_phone,
                    recipient_email, comment, total_boxes, total_units,
                    products_amount_kopecks, total_weight_grams,
                    total_volume_mm3, cargo_places,
                    request_fingerprint_digest, created_at, updated_at
                ) VALUES (
                    ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    order_id,
                    draft.buyer_type.strip(),
                    draft.buyer_contact_name.strip(),
                    draft.buyer_phone.strip(),
                    draft.buyer_email.strip(),
                    draft.company_name,
                    draft.company_inn,
                    draft.company_kpp,
                    draft.company_legal_address,
                    draft.delivery_method.strip(),
                    draft.delivery_type,
                    draft.delivery_region,
                    draft.delivery_city,
                    draft.delivery_office_code,
                    draft.delivery_postcode,
                    draft.delivery_street,
                    draft.delivery_house,
                    draft.delivery_apartment,
                    draft.recipient_contact_name,
                    draft.recipient_phone,
                    draft.recipient_email,
                    draft.comment,
                    totals.total_boxes,
                    totals.total_units,
                    totals.products_amount_kopecks,
                    totals.total_weight_grams,
                    totals.total_volume_mm3,
                    totals.cargo_places,
                    fingerprint_digest,
                    timestamp,
                    timestamp,
                ),
            )

            for line_number, snapshot in enumerate(snapshots, start=1):
                connection.execute(
                    """
                    INSERT INTO order_items (
                        order_id, line_number, sku, product_name, boxes,
                        units_per_box, units, price_per_unit_kopecks,
                        price_per_box_kopecks, line_amount_kopecks,
                        unit_weight_grams, unit_volume_mm3,
                        box_length_mm, box_width_mm, box_height_mm,
                        total_weight_grams, total_volume_mm3, cargo_places
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        line_number,
                        snapshot.sku,
                        snapshot.product_name,
                        snapshot.boxes,
                        snapshot.units_per_box,
                        snapshot.units,
                        snapshot.price_per_unit_kopecks,
                        snapshot.price_per_box_kopecks,
                        snapshot.line_amount_kopecks,
                        snapshot.unit_weight_grams,
                        snapshot.unit_volume_mm3,
                        snapshot.box_length_mm,
                        snapshot.box_width_mm,
                        snapshot.box_height_mm,
                        snapshot.total_weight_grams,
                        snapshot.total_volume_mm3,
                        snapshot.cargo_places,
                    ),
                )

            if enqueue_outbox:
                connection.execute(
                    """
                    INSERT INTO outbox (
                        event_type, order_id, status, attempt_count, available_at,
                        created_at, updated_at
                    ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (event_type, order_id, timestamp, timestamp, timestamp),
                )
            if key_digest is not None:
                connection.execute(
                    """
                    INSERT INTO idempotency_records (
                        key_digest, request_digest, order_id, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        key_digest,
                        request_digest,
                        order_id,
                        timestamp,
                        timestamp + self.idempotency_ttl_seconds,
                    ),
                )

            response = OrderResponse(
                order_id=order_id,
                status="accepted",
                created_at=timestamp,
                total_boxes=totals.total_boxes,
                total_units=totals.total_units,
                products_amount_kopecks=totals.products_amount_kopecks,
                total_weight_grams=totals.total_weight_grams,
                total_volume_mm3=totals.total_volume_mm3,
                cargo_places=totals.cargo_places,
                items=snapshots,
            )
            return CreateOrderResult(
                response=response,
                created=True,
                replayed=False,
                duplicate=False,
            )

    def get_order_response(self, order_id: str) -> Optional[OrderResponse]:
        order_id = _required_text(order_id, "order_id")
        with self.database.connection() as connection:
            return self._response_for_order(connection, order_id)

    def get_order_for_webhook(self, order_id: str) -> Optional[WebhookOrder]:
        order_id = _required_text(order_id, "order_id")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None
            return WebhookOrder(
                order_id=row["id"],
                status=row["status"],
                created_at=row["created_at"],
                buyer_type=row["buyer_type"],
                buyer_contact_name=row["buyer_contact_name"],
                buyer_phone=row["buyer_phone"],
                buyer_email=row["buyer_email"],
                company_name=row["company_name"],
                company_inn=row["company_inn"],
                company_kpp=row["company_kpp"],
                company_legal_address=row["company_legal_address"],
                delivery_method=row["delivery_method"],
                delivery_type=row["delivery_type"],
                delivery_region=row["delivery_region"],
                delivery_city=row["delivery_city"],
                delivery_office_code=row["delivery_office_code"],
                delivery_postcode=row["delivery_postcode"],
                delivery_street=row["delivery_street"],
                delivery_house=row["delivery_house"],
                delivery_apartment=row["delivery_apartment"],
                recipient_contact_name=row["recipient_contact_name"],
                recipient_phone=row["recipient_phone"],
                recipient_email=row["recipient_email"],
                comment=row["comment"],
                total_boxes=row["total_boxes"],
                total_units=row["total_units"],
                products_amount_kopecks=row["products_amount_kopecks"],
                total_weight_grams=row["total_weight_grams"],
                total_volume_mm3=row["total_volume_mm3"],
                cargo_places=row["cargo_places"],
                items=self._items_for_order(connection, order_id),
            )


class OutboxRepository:
    """Lease-based durable outbox suitable for multiple dispatcher workers."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _from_row(row: sqlite3.Row) -> OutboxMessage:
        return OutboxMessage(
            id=row["id"],
            event_type=row["event_type"],
            order_id=row["order_id"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            available_at=row["available_at"],
            locked_until=row["locked_until"],
            lock_token=row["lock_token"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _safe_error(error: object) -> str:
        # External error strings can be very large or contain control characters.
        # Dispatchers should pass a non-PII summary; this is a final storage guard.
        text = str(error).replace("\r", " ").replace("\n", " ").replace("\x00", " ")
        return text[:1_000]

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
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE outbox SET
                    status = 'failed',
                    locked_until = NULL,
                    lock_token = NULL,
                    last_error = 'Maximum delivery attempts exceeded',
                    updated_at = ?
                WHERE attempt_count >= ? AND (
                    (status = 'pending' AND available_at <= ?)
                    OR (status = 'processing' AND locked_until <= ?)
                )
                """,
                (timestamp, max_attempts, timestamp, timestamp),
            )
            candidates = connection.execute(
                """
                SELECT id FROM outbox
                WHERE attempt_count < ? AND (
                    (status = 'pending' AND available_at <= ?)
                    OR (status = 'processing' AND locked_until <= ?)
                )
                ORDER BY available_at, id
                LIMIT ?
                """,
                (max_attempts, timestamp, timestamp, limit),
            ).fetchall()

            for candidate in candidates:
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    UPDATE outbox SET
                        status = 'processing',
                        attempt_count = attempt_count + 1,
                        locked_until = ?,
                        lock_token = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp + lease_seconds, token, timestamp, candidate["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM outbox WHERE id = ?", (candidate["id"],)
                ).fetchone()
                claimed.append(self._from_row(row))
        return claimed

    def mark_success(
        self, message_id: int, lock_token: str, *, now: Optional[int] = None
    ) -> bool:
        timestamp = _now(now)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox SET
                    status = 'succeeded', locked_until = NULL, lock_token = NULL,
                    last_error = NULL, succeeded_at = ?, updated_at = ?
                WHERE id = ? AND status = 'processing' AND lock_token = ?
                """,
                (timestamp, timestamp, int(message_id), lock_token),
            )
            return cursor.rowcount == 1

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
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox SET
                    status = 'pending', available_at = ?, locked_until = NULL,
                    lock_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'processing' AND lock_token = ?
                """,
                (
                    timestamp + int(delay_seconds),
                    self._safe_error(error),
                    timestamp,
                    int(message_id),
                    lock_token,
                ),
            )
            return cursor.rowcount == 1

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
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outbox SET
                    status = 'failed', locked_until = NULL, lock_token = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'processing' AND lock_token = ?
                """,
                (
                    self._safe_error(error),
                    timestamp,
                    int(message_id),
                    lock_token,
                ),
            )
            return cursor.rowcount == 1

    fail = mark_failed

    def get(self, message_id: int) -> Optional[OutboxMessage]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outbox WHERE id = ?", (int(message_id),)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_for_order(self, order_id: str) -> Optional[OutboxMessage]:
        order_id = _required_text(order_id, "order_id")
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM outbox WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def count(self, *, status: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) FROM outbox"
        parameters: Tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        with self.database.connection() as connection:
            return int(connection.execute(query, parameters).fetchone()[0])
