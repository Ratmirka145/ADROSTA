"""add immutable invoice snapshots and PDF storage

Revision ID: b4a8d2e6f901
Revises: 7e3b1a9c5d42
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4a8d2e6f901"
down_revision: Union[str, Sequence[str], None] = "7e3b1a9c5d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoice_counters",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.CheckConstraint("year >= 2000", name="ck_invoice_counters_year"),
        sa.CheckConstraint(
            "last_value BETWEEN 1 AND 999999",
            name="ck_invoice_counters_value_range",
        ),
        sa.PrimaryKeyConstraint("year"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("invoice_number", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=False),
        sa.Column("seller_legal_name", sa.String(length=500), nullable=False),
        sa.Column("seller_inn", sa.String(length=12), nullable=False),
        sa.Column("seller_kpp", sa.String(length=9), nullable=False),
        sa.Column("seller_legal_address", sa.String(length=1000), nullable=False),
        sa.Column("seller_bank_name", sa.String(length=500), nullable=False),
        sa.Column("seller_bik", sa.String(length=9), nullable=False),
        sa.Column("seller_checking_account", sa.String(length=20), nullable=False),
        sa.Column(
            "seller_correspondent_account", sa.String(length=20), nullable=False
        ),
        sa.Column("seller_phone", sa.String(length=64), nullable=True),
        sa.Column("seller_email", sa.String(length=320), nullable=True),
        sa.Column("buyer_name", sa.String(length=500), nullable=False),
        sa.Column("buyer_inn", sa.String(length=12), nullable=True),
        sa.Column("buyer_kpp", sa.String(length=9), nullable=True),
        sa.Column("buyer_legal_address", sa.String(length=1000), nullable=True),
        sa.Column("products_amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("delivery_amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("grand_total_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("tax_text", sa.String(length=500), nullable=False),
        sa.Column("payment_purpose", sa.String(length=1000), nullable=False),
        sa.Column("pdf_content", sa.LargeBinary(), nullable=True),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("generated_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "delivery_amount_kopecks >= 0",
            name="ck_invoices_delivery_amount_non_negative",
        ),
        sa.CheckConstraint(
            "grand_total_kopecks = products_amount_kopecks + delivery_amount_kopecks",
            name="ck_invoices_grand_total_sum",
        ),
        sa.CheckConstraint(
            "(status = 'generated' AND pdf_content IS NOT NULL AND pdf_sha256 IS NOT NULL "
            "AND generated_at IS NOT NULL) OR "
            "(status <> 'generated' AND pdf_content IS NULL AND pdf_sha256 IS NULL "
            "AND generated_at IS NULL)",
            name="ck_invoices_pdf_state",
        ),
        sa.CheckConstraint(
            "products_amount_kopecks > 0",
            name="ck_invoices_products_amount_positive",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'generated', 'failed')",
            name="ck_invoices_status",
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_number", name="uq_invoices_number"),
        sa.UniqueConstraint("order_id", name="uq_invoices_order"),
    )
    op.create_index(
        "idx_invoices_status_created", "invoices", ["status", "created_at"]
    )
    op.create_table(
        "invoice_items",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("invoice_id", sa.String(length=64), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("line_type", sa.String(length=32), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("unit_price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("line_amount_kopecks", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "line_amount_kopecks = quantity * unit_price_kopecks",
            name="ck_invoice_items_amount",
        ),
        sa.CheckConstraint(
            "line_number > 0", name="ck_invoice_items_line_number_positive"
        ),
        sa.CheckConstraint(
            "line_type IN ('product', 'delivery')",
            name="ck_invoice_items_line_type",
        ),
        sa.CheckConstraint(
            "quantity > 0", name="ck_invoice_items_quantity_positive"
        ),
        sa.CheckConstraint(
            "(line_type = 'product' AND sku IS NOT NULL) OR "
            "(line_type = 'delivery' AND sku IS NULL)",
            name="ck_invoice_items_sku_by_type",
        ),
        sa.CheckConstraint(
            "unit_price_kopecks > 0", name="ck_invoice_items_unit_price_positive"
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invoice_id", "line_number", name="uq_invoice_items_line_number"
        ),
    )
    op.create_index(
        "idx_invoice_items_invoice",
        "invoice_items",
        ["invoice_id", "line_number"],
    )


def downgrade() -> None:
    op.drop_index("idx_invoice_items_invoice", table_name="invoice_items")
    op.drop_table("invoice_items")
    op.drop_index("idx_invoices_status_created", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("invoice_counters")
