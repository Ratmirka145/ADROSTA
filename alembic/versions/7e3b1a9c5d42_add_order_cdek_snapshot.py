"""add authoritative CDEK delivery snapshot to orders

Revision ID: 7e3b1a9c5d42
Revises: 1cab686b6a16
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e3b1a9c5d42"
down_revision: Union[str, Sequence[str], None] = "1cab686b6a16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("cdek_to_city_code", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("cdek_tariff_code", sa.Integer(), nullable=True))
    op.add_column(
        "orders", sa.Column("cdek_tariff_name", sa.String(length=500), nullable=True)
    )
    op.add_column("orders", sa.Column("cdek_delivery_mode", sa.Integer(), nullable=True))
    op.add_column(
        "orders", sa.Column("cdek_period_min_days", sa.Integer(), nullable=True)
    )
    op.add_column(
        "orders", sa.Column("cdek_period_max_days", sa.Integer(), nullable=True)
    )
    op.add_column(
        "orders",
        sa.Column(
            "delivery_amount_kopecks",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "orders", sa.Column("grand_total_kopecks", sa.BigInteger(), nullable=True)
    )
    op.execute(
        "UPDATE orders SET grand_total_kopecks = "
        "products_amount_kopecks + delivery_amount_kopecks"
    )
    op.alter_column(
        "orders",
        "grand_total_kopecks",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "orders",
        "delivery_amount_kopecks",
        existing_type=sa.BigInteger(),
        server_default=None,
    )

    op.create_check_constraint(
        "ck_orders_delivery_amount_non_negative",
        "orders",
        "delivery_amount_kopecks >= 0",
    )
    op.create_check_constraint(
        "ck_orders_grand_total_sum",
        "orders",
        "grand_total_kopecks = products_amount_kopecks + delivery_amount_kopecks",
    )
    op.create_check_constraint(
        "ck_orders_cdek_to_city_positive",
        "orders",
        "cdek_to_city_code IS NULL OR cdek_to_city_code > 0",
    )
    op.create_check_constraint(
        "ck_orders_cdek_tariff_positive",
        "orders",
        "cdek_tariff_code IS NULL OR cdek_tariff_code > 0",
    )
    op.create_check_constraint(
        "ck_orders_cdek_delivery_mode",
        "orders",
        "cdek_delivery_mode IS NULL OR cdek_delivery_mode BETWEEN 1 AND 4",
    )
    op.create_check_constraint(
        "ck_orders_cdek_period_min_non_negative",
        "orders",
        "cdek_period_min_days IS NULL OR cdek_period_min_days >= 0",
    )
    op.create_check_constraint(
        "ck_orders_cdek_period_range",
        "orders",
        "cdek_period_max_days IS NULL OR "
        "cdek_period_max_days >= cdek_period_min_days",
    )


def downgrade() -> None:
    for name in (
        "ck_orders_cdek_period_range",
        "ck_orders_cdek_period_min_non_negative",
        "ck_orders_cdek_delivery_mode",
        "ck_orders_cdek_tariff_positive",
        "ck_orders_cdek_to_city_positive",
        "ck_orders_grand_total_sum",
        "ck_orders_delivery_amount_non_negative",
    ):
        op.drop_constraint(name, "orders", type_="check")

    for name in (
        "grand_total_kopecks",
        "delivery_amount_kopecks",
        "cdek_period_max_days",
        "cdek_period_min_days",
        "cdek_delivery_mode",
        "cdek_tariff_name",
        "cdek_tariff_code",
        "cdek_to_city_code",
    ):
        op.drop_column("orders", name)
