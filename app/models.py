"""SQLAlchemy persistence models for the ADROSTA service."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """The single declarative metadata registry used by runtime and Alembic."""


# SQLite requires INTEGER (not BIGINT) for autoincrement primary keys. Production
# still receives BIGINT on PostgreSQL; this variant only keeps the test backend
# faithful to the same ORM/repository layer.
AUTOINCREMENT_ID = BigInteger().with_variant(Integer, "sqlite")


class ProductModel(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("length(trim(sku)) BETWEEN 1 AND 128", name="ck_products_sku_length"),
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 500", name="ck_products_name_length"),
        CheckConstraint("units_per_box > 0", name="ck_products_units_per_box_positive"),
        CheckConstraint("box_weight_grams > 0", name="ck_products_weight_positive"),
        CheckConstraint("box_volume_mm3 > 0", name="ck_products_volume_positive"),
        CheckConstraint(
            "(box_length_mm IS NULL AND box_width_mm IS NULL AND box_height_mm IS NULL) "
            "OR (box_length_mm > 0 AND box_width_mm > 0 AND box_height_mm > 0)",
            name="ck_products_dimensions_all_or_none",
        ),
        Index("idx_products_active_sku", "active", "sku"),
    )

    sku: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    units_per_box: Mapped[int] = mapped_column(Integer)
    box_weight_grams: Mapped[int] = mapped_column(BigInteger)
    box_volume_mm3: Mapped[int] = mapped_column(BigInteger)
    box_length_mm: Mapped[int | None] = mapped_column(Integer)
    box_width_mm: Mapped[int | None] = mapped_column(Integer)
    box_height_mm: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class ProductPriceTierModel(Base):
    __tablename__ = "product_price_tiers"
    __table_args__ = (
        CheckConstraint("min_boxes >= 1", name="ck_price_tiers_min_boxes_positive"),
        CheckConstraint(
            "max_boxes IS NULL OR max_boxes >= min_boxes",
            name="ck_price_tiers_valid_range",
        ),
        CheckConstraint(
            "price_per_unit_kopecks > 0", name="ck_price_tiers_price_positive"
        ),
        UniqueConstraint(
            "product_sku", "min_boxes", "max_boxes", name="uq_price_tiers_range"
        ),
        Index(
            "idx_product_price_tiers_lookup",
            "product_sku",
            "min_boxes",
            "max_boxes",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    product_sku: Mapped[str] = mapped_column(
        ForeignKey("products.sku", ondelete="CASCADE"), nullable=False
    )
    min_boxes: Mapped[int] = mapped_column(Integer)
    max_boxes: Mapped[int | None] = mapped_column(Integer)
    price_per_unit_kopecks: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class OrderModel(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("length(trim(id)) BETWEEN 1 AND 64", name="ck_orders_id_length"),
        CheckConstraint("length(trim(status)) BETWEEN 1 AND 64", name="ck_orders_status_length"),
        CheckConstraint(
            "length(trim(buyer_type)) BETWEEN 1 AND 64", name="ck_orders_buyer_type_length"
        ),
        CheckConstraint(
            "length(trim(buyer_contact_name)) BETWEEN 1 AND 500",
            name="ck_orders_buyer_name_length",
        ),
        CheckConstraint(
            "length(trim(buyer_phone)) BETWEEN 1 AND 64", name="ck_orders_buyer_phone_length"
        ),
        CheckConstraint(
            "length(trim(buyer_email)) BETWEEN 1 AND 320", name="ck_orders_buyer_email_length"
        ),
        CheckConstraint(
            "delivery_method IN ('self_pickup', 'cdek')", name="ck_orders_delivery_method"
        ),
        CheckConstraint(
            "delivery_type IS NULL OR delivery_type IN ('pickup', 'door')",
            name="ck_orders_delivery_type",
        ),
        CheckConstraint("total_boxes > 0", name="ck_orders_total_boxes_positive"),
        CheckConstraint("total_units > 0", name="ck_orders_total_units_positive"),
        CheckConstraint(
            "products_amount_kopecks > 0", name="ck_orders_products_amount_positive"
        ),
        CheckConstraint(
            "total_weight_grams > 0", name="ck_orders_total_weight_positive"
        ),
        CheckConstraint("cargo_places > 0", name="ck_orders_cargo_places_positive"),
        CheckConstraint(
            "total_volume_mm3 > 0", name="ck_orders_total_volume_positive"
        ),
        Index("idx_orders_fingerprint_created", "request_fingerprint_digest", "created_at"),
        Index("idx_orders_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(64))
    buyer_type: Mapped[str] = mapped_column(String(64))
    buyer_contact_name: Mapped[str] = mapped_column(String(500))
    buyer_phone: Mapped[str] = mapped_column(String(64))
    buyer_email: Mapped[str] = mapped_column(String(320))
    company_name: Mapped[str | None] = mapped_column(String(500))
    company_inn: Mapped[str | None] = mapped_column(String(12))
    company_kpp: Mapped[str | None] = mapped_column(String(9))
    company_legal_address: Mapped[str | None] = mapped_column(String(1000))
    delivery_method: Mapped[str] = mapped_column(String(128))
    delivery_type: Mapped[str | None] = mapped_column(String(32))
    delivery_region: Mapped[str | None] = mapped_column(String(500))
    delivery_city: Mapped[str | None] = mapped_column(String(500))
    delivery_office_code: Mapped[str | None] = mapped_column(String(128))
    delivery_postcode: Mapped[str | None] = mapped_column(String(32))
    delivery_street: Mapped[str | None] = mapped_column(String(500))
    delivery_house: Mapped[str | None] = mapped_column(String(128))
    delivery_apartment: Mapped[str | None] = mapped_column(String(128))
    recipient_contact_name: Mapped[str | None] = mapped_column(String(500))
    recipient_phone: Mapped[str | None] = mapped_column(String(64))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    comment: Mapped[str | None] = mapped_column(Text)
    total_boxes: Mapped[int] = mapped_column(Integer)
    total_units: Mapped[int] = mapped_column(BigInteger)
    products_amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    total_weight_grams: Mapped[int] = mapped_column(BigInteger)
    total_volume_mm3: Mapped[int] = mapped_column(BigInteger)
    cargo_places: Mapped[int] = mapped_column(Integer)
    request_fingerprint_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class OrderItemModel(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("line_number > 0", name="ck_order_items_line_number_positive"),
        CheckConstraint("boxes > 0", name="ck_order_items_boxes_positive"),
        CheckConstraint("units_per_box > 0", name="ck_order_items_units_per_box_positive"),
        CheckConstraint("units > 0", name="ck_order_items_units_positive"),
        CheckConstraint(
            "price_per_unit_kopecks > 0", name="ck_order_items_unit_price_positive"
        ),
        CheckConstraint(
            "price_per_box_kopecks > 0", name="ck_order_items_box_price_positive"
        ),
        CheckConstraint("line_amount_kopecks > 0", name="ck_order_items_amount_positive"),
        CheckConstraint("unit_weight_grams > 0", name="ck_order_items_unit_weight_positive"),
        CheckConstraint("unit_volume_mm3 > 0", name="ck_order_items_unit_volume_positive"),
        CheckConstraint(
            "total_weight_grams > 0", name="ck_order_items_total_weight_positive"
        ),
        CheckConstraint(
            "total_volume_mm3 > 0", name="ck_order_items_total_volume_positive"
        ),
        CheckConstraint("cargo_places > 0", name="ck_order_items_cargo_places_positive"),
        CheckConstraint(
            "(box_length_mm IS NULL AND box_width_mm IS NULL AND box_height_mm IS NULL) "
            "OR (box_length_mm > 0 AND box_width_mm > 0 AND box_height_mm > 0)",
            name="ck_order_items_dimensions_all_or_none",
        ),
        UniqueConstraint("order_id", "line_number", name="uq_order_items_line_number"),
        UniqueConstraint("order_id", "sku", name="uq_order_items_sku"),
        Index("idx_order_items_order", "order_id", "line_number"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(128))
    product_name: Mapped[str] = mapped_column(String(500))
    boxes: Mapped[int] = mapped_column(Integer)
    units_per_box: Mapped[int] = mapped_column(Integer)
    units: Mapped[int] = mapped_column(BigInteger)
    price_per_unit_kopecks: Mapped[int] = mapped_column(BigInteger)
    price_per_box_kopecks: Mapped[int] = mapped_column(BigInteger)
    line_amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    unit_weight_grams: Mapped[int] = mapped_column(BigInteger)
    unit_volume_mm3: Mapped[int] = mapped_column(BigInteger)
    box_length_mm: Mapped[int | None] = mapped_column(Integer)
    box_width_mm: Mapped[int | None] = mapped_column(Integer)
    box_height_mm: Mapped[int | None] = mapped_column(Integer)
    total_weight_grams: Mapped[int] = mapped_column(BigInteger)
    total_volume_mm3: Mapped[int] = mapped_column(BigInteger)
    cargo_places: Mapped[int] = mapped_column(Integer)


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="ck_idempotency_expiry"),
        Index("idx_idempotency_expires", "expires_at"),
    )

    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)


class RateLimitWindowModel(Base):
    __tablename__ = "rate_limit_windows"
    __table_args__ = (
        CheckConstraint("request_count > 0", name="ck_rate_limit_count_positive"),
        Index("idx_rate_limit_window_start", "window_start"),
    )

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_count: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class OutboxModel(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed')",
            name="ck_outbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_outbox_attempt_count"),
        CheckConstraint(
            "(status = 'processing' AND lock_token IS NOT NULL AND locked_until IS NOT NULL) "
            "OR status <> 'processing'",
            name="ck_outbox_processing_lock",
        ),
        UniqueConstraint("event_type", "order_id", name="uq_outbox_event_order"),
        Index("idx_outbox_claim", "status", "available_at", "locked_until", "id"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128))
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32))
    attempt_count: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[int] = mapped_column(BigInteger)
    locked_until: Mapped[int | None] = mapped_column(BigInteger)
    lock_token: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    succeeded_at: Mapped[int | None] = mapped_column(BigInteger)


__all__ = [
    "Base",
    "IdempotencyRecordModel",
    "OrderItemModel",
    "OrderModel",
    "OutboxModel",
    "ProductModel",
    "ProductPriceTierModel",
    "RateLimitWindowModel",
]
