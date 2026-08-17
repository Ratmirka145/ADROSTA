"""add customer sessions, order grants and public order numbers

Revision ID: c9e1f4a7d203
Revises: b4a8d2e6f901
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9e1f4a7d203"
down_revision: Union[str, Sequence[str], None] = "b4a8d2e6f901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_counters",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.CheckConstraint("year >= 2000", name="ck_order_counters_year"),
        sa.CheckConstraint(
            "last_value BETWEEN 1 AND 999999",
            name="ck_order_counters_value_range",
        ),
        sa.PrimaryKeyConstraint("year"),
    )
    op.add_column(
        "orders",
        sa.Column("order_number", sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        WITH numbered AS (
            SELECT
                id,
                EXTRACT(YEAR FROM to_timestamp(created_at) AT TIME ZONE 'UTC')::integer
                    AS order_year,
                row_number() OVER (
                    PARTITION BY EXTRACT(
                        YEAR FROM to_timestamp(created_at) AT TIME ZONE 'UTC'
                    )
                    ORDER BY created_at, id
                ) AS order_sequence
            FROM orders
        )
        UPDATE orders AS target
        SET order_number =
            'AD-' || numbered.order_year::text || '-' ||
            lpad(numbered.order_sequence::text, 6, '0')
        FROM numbered
        WHERE target.id = numbered.id
        """
    )
    op.execute(
        """
        INSERT INTO order_counters (year, last_value)
        SELECT
            EXTRACT(YEAR FROM to_timestamp(created_at) AT TIME ZONE 'UTC')::integer,
            count(*)::integer
        FROM orders
        GROUP BY EXTRACT(YEAR FROM to_timestamp(created_at) AT TIME ZONE 'UTC')
        """
    )
    op.alter_column(
        "orders",
        "order_number",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_orders_number_length",
        "orders",
        "length(trim(order_number)) BETWEEN 12 AND 32",
    )
    op.create_unique_constraint("uq_orders_number", "orders", ["order_number"])

    op.create_table(
        "customer_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="ck_customer_sessions_token_hash_length",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_customer_sessions_expiry",
        ),
        sa.CheckConstraint(
            "last_used_at >= created_at",
            name="ck_customer_sessions_last_used",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_customer_sessions_revoked",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_customer_sessions_token_hash"),
    )
    op.create_index(
        "idx_customer_sessions_expires",
        "customer_sessions",
        ["expires_at"],
    )
    op.create_table(
        "customer_session_orders",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["customer_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "order_id"),
    )
    op.create_index(
        "idx_customer_session_orders_order",
        "customer_session_orders",
        ["order_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_customer_session_orders_order",
        table_name="customer_session_orders",
    )
    op.drop_table("customer_session_orders")
    op.drop_index("idx_customer_sessions_expires", table_name="customer_sessions")
    op.drop_table("customer_sessions")
    op.drop_constraint("uq_orders_number", "orders", type_="unique")
    op.drop_constraint("ck_orders_number_length", "orders", type_="check")
    op.drop_column("orders", "order_number")
    op.drop_table("order_counters")
