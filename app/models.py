"""SQLAlchemy persistence models for the ADROSTA service."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
        CheckConstraint(
            "length(trim(order_number)) BETWEEN 12 AND 32",
            name="ck_orders_number_length",
        ),
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
            "delivery_amount_kopecks >= 0",
            name="ck_orders_delivery_amount_non_negative",
        ),
        CheckConstraint(
            "grand_total_kopecks = products_amount_kopecks + delivery_amount_kopecks",
            name="ck_orders_grand_total_sum",
        ),
        CheckConstraint(
            "cdek_to_city_code IS NULL OR cdek_to_city_code > 0",
            name="ck_orders_cdek_to_city_positive",
        ),
        CheckConstraint(
            "cdek_tariff_code IS NULL OR cdek_tariff_code > 0",
            name="ck_orders_cdek_tariff_positive",
        ),
        CheckConstraint(
            "cdek_delivery_mode IS NULL OR cdek_delivery_mode BETWEEN 1 AND 4",
            name="ck_orders_cdek_delivery_mode",
        ),
        CheckConstraint(
            "cdek_period_min_days IS NULL OR cdek_period_min_days >= 0",
            name="ck_orders_cdek_period_min_non_negative",
        ),
        CheckConstraint(
            "cdek_period_max_days IS NULL OR cdek_period_max_days >= cdek_period_min_days",
            name="ck_orders_cdek_period_range",
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
        UniqueConstraint("order_number", name="uq_orders_number"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_number: Mapped[str] = mapped_column(String(32))
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
    cdek_to_city_code: Mapped[int | None] = mapped_column(Integer)
    cdek_tariff_code: Mapped[int | None] = mapped_column(Integer)
    cdek_tariff_name: Mapped[str | None] = mapped_column(String(500))
    cdek_delivery_mode: Mapped[int | None] = mapped_column(Integer)
    cdek_period_min_days: Mapped[int | None] = mapped_column(Integer)
    cdek_period_max_days: Mapped[int | None] = mapped_column(Integer)
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
    delivery_amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    grand_total_kopecks: Mapped[int] = mapped_column(BigInteger)
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


class InvoiceCounterModel(Base):
    __tablename__ = "invoice_counters"
    __table_args__ = (
        CheckConstraint("year >= 2000", name="ck_invoice_counters_year"),
        CheckConstraint(
            "last_value BETWEEN 1 AND 999999",
            name="ck_invoice_counters_value_range",
        ),
    )

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_value: Mapped[int] = mapped_column(Integer)


class InvoiceModel(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'generated', 'failed')",
            name="ck_invoices_status",
        ),
        CheckConstraint(
            "products_amount_kopecks > 0",
            name="ck_invoices_products_amount_positive",
        ),
        CheckConstraint(
            "delivery_amount_kopecks >= 0",
            name="ck_invoices_delivery_amount_non_negative",
        ),
        CheckConstraint(
            "grand_total_kopecks = products_amount_kopecks + delivery_amount_kopecks",
            name="ck_invoices_grand_total_sum",
        ),
        CheckConstraint(
            "(status = 'generated' AND pdf_content IS NOT NULL AND pdf_sha256 IS NOT NULL "
            "AND generated_at IS NOT NULL) OR "
            "(status <> 'generated' AND pdf_content IS NULL AND pdf_sha256 IS NULL "
            "AND generated_at IS NULL)",
            name="ck_invoices_pdf_state",
        ),
        UniqueConstraint("order_id", name="uq_invoices_order"),
        UniqueConstraint("invoice_number", name="uq_invoices_number"),
        Index("idx_invoices_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(String(32))
    issued_at: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32))
    template_version: Mapped[str] = mapped_column(String(64))
    seller_legal_name: Mapped[str] = mapped_column(String(500))
    seller_inn: Mapped[str] = mapped_column(String(12))
    seller_kpp: Mapped[str] = mapped_column(String(9))
    seller_legal_address: Mapped[str] = mapped_column(String(1000))
    seller_bank_name: Mapped[str] = mapped_column(String(500))
    seller_bik: Mapped[str] = mapped_column(String(9))
    seller_checking_account: Mapped[str] = mapped_column(String(20))
    seller_correspondent_account: Mapped[str] = mapped_column(String(20))
    seller_phone: Mapped[str | None] = mapped_column(String(64))
    seller_email: Mapped[str | None] = mapped_column(String(320))
    buyer_name: Mapped[str] = mapped_column(String(500))
    buyer_inn: Mapped[str | None] = mapped_column(String(12))
    buyer_kpp: Mapped[str | None] = mapped_column(String(9))
    buyer_legal_address: Mapped[str | None] = mapped_column(String(1000))
    products_amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    delivery_amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    grand_total_kopecks: Mapped[int] = mapped_column(BigInteger)
    tax_text: Mapped[str] = mapped_column(String(500))
    payment_purpose: Mapped[str] = mapped_column(String(1000))
    pdf_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    generated_at: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class InvoiceItemModel(Base):
    __tablename__ = "invoice_items"
    __table_args__ = (
        CheckConstraint("line_number > 0", name="ck_invoice_items_line_number_positive"),
        CheckConstraint(
            "line_type IN ('product', 'delivery')",
            name="ck_invoice_items_line_type",
        ),
        CheckConstraint("quantity > 0", name="ck_invoice_items_quantity_positive"),
        CheckConstraint(
            "unit_price_kopecks > 0", name="ck_invoice_items_unit_price_positive"
        ),
        CheckConstraint(
            "line_amount_kopecks = quantity * unit_price_kopecks",
            name="ck_invoice_items_amount",
        ),
        CheckConstraint(
            "(line_type = 'product' AND sku IS NOT NULL) OR "
            "(line_type = 'delivery' AND sku IS NULL)",
            name="ck_invoice_items_sku_by_type",
        ),
        UniqueConstraint("invoice_id", "line_number", name="uq_invoice_items_line_number"),
        Index("idx_invoice_items_invoice", "invoice_id", "line_number"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(32))
    sku: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(32))
    unit_price_kopecks: Mapped[int] = mapped_column(BigInteger)
    line_amount_kopecks: Mapped[int] = mapped_column(BigInteger)


class OrderCounterModel(Base):
    __tablename__ = "order_counters"
    __table_args__ = (
        CheckConstraint("year >= 2000", name="ck_order_counters_year"),
        CheckConstraint(
            "last_value BETWEEN 1 AND 999999",
            name="ck_order_counters_value_range",
        ),
    )

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_value: Mapped[int] = mapped_column(Integer)


class CustomerSessionModel(Base):
    __tablename__ = "customer_sessions"
    __table_args__ = (
        CheckConstraint(
            "length(token_hash) = 64",
            name="ck_customer_sessions_token_hash_length",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_customer_sessions_expiry",
        ),
        CheckConstraint(
            "last_used_at >= created_at",
            name="ck_customer_sessions_last_used",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_customer_sessions_revoked",
        ),
        UniqueConstraint("token_hash", name="uq_customer_sessions_token_hash"),
        Index("idx_customer_sessions_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    last_used_at: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger)


class CustomerSessionOrderModel(Base):
    __tablename__ = "customer_session_orders"
    __table_args__ = (
        Index("idx_customer_session_orders_order", "order_id"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("customer_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    order_id: Mapped[str] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[int] = mapped_column(BigInteger)


__all__ = [
    "Base",
    "CustomerSessionModel",
    "CustomerSessionOrderModel",
    "IdempotencyRecordModel",
    "InvoiceCounterModel",
    "InvoiceItemModel",
    "InvoiceModel",
    "OrderItemModel",
    "OrderModel",
    "OrderCounterModel",
    "OutboxModel",
    "ProductModel",
    "ProductPriceTierModel",
    "RateLimitWindowModel",
]
