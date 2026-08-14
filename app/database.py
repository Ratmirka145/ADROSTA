"""SQLite connection management and schema for the ADROSTA order service.

The module deliberately depends only on Python's standard library.  Every
connection enables foreign keys and a busy timeout; file-backed databases use
WAL mode so readers do not block the short write transactions used by the
repositories.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union


SCHEMA_VERSION = 3


_DELIVERY_V2_COLUMNS = {
    "delivery_type": "TEXT",
    "delivery_office_code": "TEXT",
    "delivery_postcode": "TEXT",
    "delivery_street": "TEXT",
    "delivery_house": "TEXT",
    "delivery_apartment": "TEXT",
}

_CATALOG_V3_COLUMNS = {
    "products": {
        "units_per_box": (
            "INTEGER NOT NULL DEFAULT 1 "
            "CHECK (typeof(units_per_box) = 'integer' AND units_per_box > 0)"
        ),
    },
    "orders": {
        "total_units": "INTEGER NOT NULL DEFAULT 0",
        "products_amount_kopecks": "INTEGER NOT NULL DEFAULT 0",
        "cargo_places": "INTEGER NOT NULL DEFAULT 0",
    },
    "order_items": {
        "units_per_box": "INTEGER NOT NULL DEFAULT 1",
        "units": "INTEGER NOT NULL DEFAULT 0",
        "price_per_unit_kopecks": "INTEGER NOT NULL DEFAULT 0",
        "price_per_box_kopecks": "INTEGER NOT NULL DEFAULT 0",
        "line_amount_kopecks": "INTEGER NOT NULL DEFAULT 0",
        "cargo_places": "INTEGER NOT NULL DEFAULT 0",
    },
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    units_per_box INTEGER NOT NULL
        CHECK (typeof(units_per_box) = 'integer' AND units_per_box > 0),
    box_weight_grams INTEGER NOT NULL
        CHECK (typeof(box_weight_grams) = 'integer' AND box_weight_grams > 0),
    box_volume_mm3 INTEGER NOT NULL
        CHECK (typeof(box_volume_mm3) = 'integer' AND box_volume_mm3 > 0),
    box_length_mm INTEGER,
    box_width_mm INTEGER,
    box_height_mm INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (length(trim(sku)) BETWEEN 1 AND 128),
    CHECK (length(trim(name)) BETWEEN 1 AND 500),
    CHECK (
        (box_length_mm IS NULL AND box_width_mm IS NULL AND box_height_mm IS NULL)
        OR
        (box_length_mm > 0 AND box_width_mm > 0 AND box_height_mm > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_products_active_sku
    ON products(active, sku);

CREATE TABLE IF NOT EXISTS product_price_tiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    min_boxes INTEGER NOT NULL
        CHECK (typeof(min_boxes) = 'integer' AND min_boxes >= 1),
    max_boxes INTEGER
        CHECK (
            max_boxes IS NULL OR
            (typeof(max_boxes) = 'integer' AND max_boxes >= min_boxes)
        ),
    price_per_unit_kopecks INTEGER NOT NULL
        CHECK (
            typeof(price_per_unit_kopecks) = 'integer' AND
            price_per_unit_kopecks > 0
        ),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(product_sku, min_boxes, max_boxes)
);

CREATE INDEX IF NOT EXISTS idx_product_price_tiers_lookup
    ON product_price_tiers(product_sku, min_boxes, max_boxes);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    buyer_type TEXT NOT NULL,
    buyer_contact_name TEXT NOT NULL,
    buyer_phone TEXT NOT NULL,
    buyer_email TEXT NOT NULL,
    company_name TEXT,
    company_inn TEXT,
    company_kpp TEXT,
    company_legal_address TEXT,
    delivery_method TEXT NOT NULL CHECK (delivery_method IN ('self_pickup', 'cdek')),
    delivery_type TEXT CHECK (delivery_type IS NULL OR delivery_type IN ('pickup', 'door')),
    delivery_region TEXT,
    delivery_city TEXT,
    delivery_office_code TEXT,
    delivery_postcode TEXT,
    delivery_street TEXT,
    delivery_house TEXT,
    delivery_apartment TEXT,
    recipient_contact_name TEXT,
    recipient_phone TEXT,
    recipient_email TEXT,
    comment TEXT,
    total_boxes INTEGER NOT NULL
        CHECK (typeof(total_boxes) = 'integer' AND total_boxes > 0),
    total_units INTEGER NOT NULL
        CHECK (typeof(total_units) = 'integer' AND total_units > 0),
    products_amount_kopecks INTEGER NOT NULL
        CHECK (
            typeof(products_amount_kopecks) = 'integer' AND
            products_amount_kopecks > 0
        ),
    total_weight_grams INTEGER NOT NULL
        CHECK (typeof(total_weight_grams) = 'integer' AND total_weight_grams > 0),
    cargo_places INTEGER NOT NULL
        CHECK (typeof(cargo_places) = 'integer' AND cargo_places > 0),
    total_volume_mm3 INTEGER NOT NULL
        CHECK (typeof(total_volume_mm3) = 'integer' AND total_volume_mm3 > 0),
    request_fingerprint_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK (length(trim(id)) BETWEEN 1 AND 64),
    CHECK (length(trim(status)) BETWEEN 1 AND 64),
    CHECK (length(trim(buyer_type)) BETWEEN 1 AND 64),
    CHECK (length(trim(buyer_contact_name)) BETWEEN 1 AND 500),
    CHECK (length(trim(buyer_phone)) BETWEEN 1 AND 64),
    CHECK (length(trim(buyer_email)) BETWEEN 1 AND 320),
    CHECK (length(trim(delivery_method)) BETWEEN 1 AND 128)
);

CREATE INDEX IF NOT EXISTS idx_orders_fingerprint_created
    ON orders(request_fingerprint_digest, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_created
    ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    boxes INTEGER NOT NULL
        CHECK (typeof(boxes) = 'integer' AND boxes > 0),
    units_per_box INTEGER NOT NULL
        CHECK (typeof(units_per_box) = 'integer' AND units_per_box > 0),
    units INTEGER NOT NULL
        CHECK (typeof(units) = 'integer' AND units > 0),
    price_per_unit_kopecks INTEGER NOT NULL
        CHECK (
            typeof(price_per_unit_kopecks) = 'integer' AND
            price_per_unit_kopecks > 0
        ),
    price_per_box_kopecks INTEGER NOT NULL
        CHECK (
            typeof(price_per_box_kopecks) = 'integer' AND
            price_per_box_kopecks > 0
        ),
    line_amount_kopecks INTEGER NOT NULL
        CHECK (
            typeof(line_amount_kopecks) = 'integer' AND
            line_amount_kopecks > 0
        ),
    unit_weight_grams INTEGER NOT NULL
        CHECK (typeof(unit_weight_grams) = 'integer' AND unit_weight_grams > 0),
    unit_volume_mm3 INTEGER NOT NULL
        CHECK (typeof(unit_volume_mm3) = 'integer' AND unit_volume_mm3 > 0),
    box_length_mm INTEGER,
    box_width_mm INTEGER,
    box_height_mm INTEGER,
    total_weight_grams INTEGER NOT NULL
        CHECK (typeof(total_weight_grams) = 'integer' AND total_weight_grams > 0),
    total_volume_mm3 INTEGER NOT NULL
        CHECK (typeof(total_volume_mm3) = 'integer' AND total_volume_mm3 > 0),
    cargo_places INTEGER NOT NULL
        CHECK (typeof(cargo_places) = 'integer' AND cargo_places > 0),
    UNIQUE(order_id, line_number),
    UNIQUE(order_id, sku),
    CHECK (
        (box_length_mm IS NULL AND box_width_mm IS NULL AND box_height_mm IS NULL)
        OR
        (box_length_mm > 0 AND box_width_mm > 0 AND box_height_mm > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_order_items_order
    ON order_items(order_id, line_number);

CREATE TABLE IF NOT EXISTS idempotency_records (
    key_digest TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_expires
    ON idempotency_records(expires_at);

CREATE TABLE IF NOT EXISTS rate_limit_windows (
    scope TEXT NOT NULL,
    subject_digest TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL
        CHECK (typeof(request_count) = 'integer' AND request_count > 0),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(scope, subject_digest, window_start)
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_window_start
    ON rate_limit_windows(window_start);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at INTEGER NOT NULL,
    locked_until INTEGER,
    lock_token TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    succeeded_at INTEGER,
    UNIQUE(event_type, order_id),
    CHECK (
        (status = 'processing' AND locked_until IS NOT NULL AND lock_token IS NOT NULL)
        OR status <> 'processing'
    )
);

CREATE INDEX IF NOT EXISTS idx_outbox_claim
    ON outbox(status, available_at, locked_until, id);
"""


class Database:
    """A small connection factory with explicit transaction boundaries."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")

        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._uri = self.path.startswith("file:")
        self._anchor: Optional[sqlite3.Connection] = None
        self._anchor_lock = threading.Lock()

        # A shared in-memory URI keeps tests deterministic while retaining the
        # normal one-connection-per-operation repository behaviour.
        if self.path == ":memory:":
            self.path = f"file:adrosta-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._anchor = self._open_connection()

    def _prepare_parent_directory(self) -> None:
        if self._uri or self.path == ":memory:":
            return
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA synchronous = NORMAL")
        if not (self._uri and "mode=memory" in self.path):
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        """Return a configured connection owned by the caller."""

        self._prepare_parent_directory()
        return self._open_connection()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run a transaction and always close its connection.

        ``BEGIN IMMEDIATE`` is the default because all repository transactions
        are short and must serialize read-modify-write decisions (rate limits,
        idempotency and outbox claims).
        """

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the schema idempotently and record its version."""

        with self._anchor_lock:
            with self.connection() as connection:
                connection.executescript(SCHEMA_SQL)
                order_columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(orders)")
                }
                for name, column_type in _DELIVERY_V2_COLUMNS.items():
                    if name not in order_columns:
                        connection.execute(
                            f"ALTER TABLE orders ADD COLUMN {name} {column_type}"
                        )
                for table, columns in _CATALOG_V3_COLUMNS.items():
                    existing = {
                        row["name"]
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    for name, column_type in columns.items():
                        if name not in existing:
                            connection.execute(
                                f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
                            )
                # Historical v2 rows had no units, price or cargo snapshots.
                # Backfill only derivable values; unavailable historical prices
                # remain zero instead of being guessed from today's catalog.
                connection.execute(
                    """
                    UPDATE order_items SET
                        units = boxes * units_per_box,
                        cargo_places = boxes
                    WHERE units = 0 OR cargo_places = 0
                    """
                )
                connection.execute(
                    """
                    UPDATE orders SET
                        total_units = COALESCE((
                            SELECT SUM(units) FROM order_items
                            WHERE order_items.order_id = orders.id
                        ), 0),
                        cargo_places = total_boxes
                    WHERE total_units = 0 OR cargo_places = 0
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    def close(self) -> None:
        """Release the anchor used only by shared in-memory databases."""

        with self._anchor_lock:
            if self._anchor is not None:
                self._anchor.close()
                self._anchor = None

    def __enter__(self) -> "Database":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def initialize_database(
    path: Union[str, Path],
    *,
    busy_timeout_ms: int = 5_000,
) -> Database:
    """Convenience factory used by application startup and tests."""

    database = Database(path, busy_timeout_ms=busy_timeout_ms)
    database.initialize()
    return database
