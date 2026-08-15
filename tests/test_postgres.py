from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
import os
import subprocess
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.engine import make_url

from app.config import Settings
from app.database import Database
from app.domain import calculate_order
from app.main import create_app
from app.models import (
    IdempotencyRecordModel,
    OrderItemModel,
    OrderModel,
    OutboxModel,
    ProductModel,
    ProductPriceTierModel,
    RateLimitWindowModel,
)
from app.repositories import OrderDraft, OrderItemInput, Product, ProductPriceTier


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="TEST_DATABASE_URL is not set to a disposable PostgreSQL database",
    ),
]


class PostgresFakeCdekClient:
    def tariff_list(
        self, payload: Mapping[str, Any], **kwargs: object
    ) -> list[Mapping[str, Any]]:
        assert payload["to_location"] == {"code": 44}
        assert kwargs["request_id"]
        return [
            {
                "tariff_code": 136,
                "tariff_name": "Посылка склад-склад",
                "delivery_mode": 4,
                "delivery_sum": "1234.50",
                "period_min": 2,
                "period_max": 4,
            }
        ]

    def delivery_points(self, **kwargs: object) -> list[Mapping[str, Any]]:
        assert kwargs["city_code"] == 44
        assert kwargs["request_id"]
        return [
            {
                "code": "MSK123",
                "name": "ПВЗ Тест",
                "type": "PVZ",
                "location": {
                    "address": "Москва, ул. Тестовая, 1",
                    "city_code": 44,
                },
            }
        ]


def _settings() -> Settings:
    if make_url(TEST_DATABASE_URL).drivername != "postgresql+psycopg":
        pytest.fail("TEST_DATABASE_URL must use postgresql+psycopg")
    return Settings.from_env(
        {
            "APP_ENV": "test",
            "DEBUG": "false",
            "API_DOCS_ENABLED": "false",
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": TEST_DATABASE_URL,
            "APP_HASH_SECRET": "postgres-integration-secret-never-use-in-production",
            "ORDER_RATE_LIMIT_COUNT": "1000",
            "WEBHOOK_ENABLED": "true",
            "ORDER_WEBHOOK_URL": "http://127.0.0.1:9/orders",
            "OUTBOX_ENABLED": "true",
            "ALLOW_STORE_ONLY": "true",
        },
        load_env_file=False,
    )


def _reset_database() -> None:
    environment = dict(os.environ)
    environment["DATABASE_URL"] = TEST_DATABASE_URL
    subprocess.run(
        ["python", "-m", "alembic", "upgrade", "head"],
        check=True,
        env=environment,
    )
    database = Database(TEST_DATABASE_URL)
    try:
        with database.transaction() as session:
            for model in (
                OutboxModel,
                IdempotencyRecordModel,
                OrderItemModel,
                RateLimitWindowModel,
                ProductPriceTierModel,
                OrderModel,
                ProductModel,
            ):
                session.execute(delete(model))
    finally:
        database.close()


def _seed(application) -> None:
    products = application.state.context.products
    tiers = (
        ProductPriceTier(1, 4, 32_000),
        ProductPriceTier(5, 9, 29_000),
        ProductPriceTier(10, None, 26_000),
    )
    for sku, name in (
        ("opt-san-green", "SAN Green"),
        ("opt-san-blue", "SAN Blue"),
    ):
        products.upsert(
            Product(
                sku=sku,
                name=name,
                units_per_box=10,
                box_weight_grams=10_900,
                box_volume_mm3=17_490_000,
                box_length_mm=330,
                box_width_mm=200,
                box_height_mm=265,
            )
        )
        products.replace_price_tiers(sku, tiers)


@pytest.fixture
def postgres_app():
    _reset_database()
    application = create_app(_settings())
    _seed(application)
    _seed(application)
    yield application
    application.state.context.close()


def test_postgresql_migration_seed_cart_order_and_outbox(postgres_app) -> None:
    context = postgres_app.state.context
    assert set(inspect(context.database.engine).get_table_names()) == {
        "alembic_version",
        "products",
        "product_price_tiers",
        "orders",
        "order_items",
        "idempotency_records",
        "rate_limit_windows",
        "outbox",
    }
    with context.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ProductModel)) == 2
        assert (
            session.scalar(select(func.count()).select_from(ProductPriceTierModel))
            == 6
        )

    with TestClient(postgres_app) as client:
        cart = client.post(
            "/api/cart/calculate",
            json={"items": [{"sku": "opt-san-green", "boxes": 5}]},
        )
        order = client.post(
            "/api/orders",
            headers={"Idempotency-Key": "postgres-order-key-0001"},
            json={
                "buyer": {
                    "type": "individual",
                    "contactName": "Иван Петров",
                    "phone": "8 (999) 123-45-67",
                    "email": "buyer@example.com",
                },
                "delivery": {"method": "self_pickup"},
                "items": [{"sku": "opt-san-green", "boxes": 5}],
            },
        )

    assert cart.status_code == 200
    assert cart.json()["totals"] == {
        "totalBoxes": 5,
        "totalUnits": 50,
        "productsAmountKopecks": 1_450_000,
        "totalWeightGrams": 54_500,
        "cargoPlaces": 5,
        "totalVolumeMm3": 87_450_000,
    }
    assert order.status_code == 201
    message = context.outbox.get_for_order(order.json()["orderId"])
    assert message is not None
    assert message.status == "pending"
    claimed = context.outbox.claim(limit=1)
    assert [item.id for item in claimed] == [message.id]


def test_postgresql_persists_verified_cdek_delivery_snapshot(postgres_app) -> None:
    context = postgres_app.state.context
    context.cdek_service.client = PostgresFakeCdekClient()
    context.cdek_service.from_city_code = 137
    with TestClient(postgres_app) as client:
        response = client.post(
            "/api/orders",
            headers={"Idempotency-Key": "postgres-cdek-order-key-0001"},
            json={
                "buyer": {
                    "type": "individual",
                    "contactName": "Иван Петров",
                    "phone": "8 (999) 123-45-67",
                    "email": "buyer@example.com",
                },
                "delivery": {
                    "method": "cdek",
                    "type": "pickup",
                    "toCityCode": 44,
                    "tariffCode": 136,
                    "officeCode": "MSK123",
                },
                "items": [{"sku": "opt-san-green", "boxes": 5}],
            },
        )

    assert response.status_code == 201
    assert response.json()["totals"]["productsAmountKopecks"] == 1_450_000
    assert response.json()["totals"]["deliveryAmountKopecks"] == 123_450
    assert response.json()["totals"]["grandTotalKopecks"] == 1_573_450
    with context.database.session() as session:
        order = session.get(OrderModel, response.json()["orderId"])
        assert order is not None
        assert order.cdek_to_city_code == 44
        assert order.cdek_tariff_code == 136
        assert order.cdek_tariff_name == "Посылка склад-склад"
        assert order.cdek_delivery_mode == 4
        assert order.delivery_office_code == "MSK123"
        assert order.cdek_period_min_days == 2
        assert order.cdek_period_max_days == 4
        assert order.delivery_amount_kopecks == 123_450
        assert order.grand_total_kopecks == 1_573_450
    assert context.orders is not None
    webhook_snapshot = context.orders.get_order_for_webhook(
        response.json()["orderId"]
    )
    assert webhook_snapshot is not None
    assert webhook_snapshot.as_dict()["delivery"]["tariffCode"] == 136
    assert webhook_snapshot.as_dict()["totals"]["grandTotalKopecks"] == 1_573_450


def test_postgresql_concurrent_idempotency_creates_one_order(postgres_app) -> None:
    context = postgres_app.state.context
    assert context.orders is not None
    items = (OrderItemInput(sku="opt-san-green", boxes=5),)
    calculation = calculate_order(items, context.products.fetch_catalog((items[0].sku,)))
    draft = OrderDraft(
        buyer_type="individual",
        buyer_contact_name="Иван Петров",
        buyer_phone="+79991234567",
        buyer_email="buyer@example.com",
        delivery_method="self_pickup",
        items=items,
    )

    def create_once():
        return context.orders.create_order(
            draft,
            calculation=calculation,
            request_hash="same-concurrent-request",
            duplicate_fingerprint="same-concurrent-fingerprint",
            idempotency_key="postgres-concurrent-key-0001",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_once(), range(2)))

    assert sorted(result.disposition for result in results) == ["created", "replayed"]
    assert results[0].response.order_id == results[1].response.order_id
    with context.database.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderModel)) == 1
        assert (
            session.scalar(select(func.count()).select_from(IdempotencyRecordModel))
            == 1
        )
        assert session.scalar(select(func.count()).select_from(OutboxModel)) == 1


def test_postgresql_concurrent_outbox_claims_do_not_overlap(postgres_app) -> None:
    context = postgres_app.state.context
    payload = {
        "buyer": {
            "type": "individual",
            "contactName": "Иван Петров",
            "phone": "8 (999) 123-45-67",
            "email": "buyer@example.com",
        },
        "delivery": {"method": "self_pickup"},
        "items": [{"sku": "opt-san-green", "boxes": 5}],
    }
    with TestClient(postgres_app) as client:
        for number in (1, 2):
            response = client.post(
                "/api/orders",
                headers={"Idempotency-Key": f"outbox-claim-key-{number:04d}"},
                json={**payload, "comment": f"order {number}"},
            )
            assert response.status_code == 201

    barrier = Barrier(2)

    def claim_one():
        barrier.wait()
        return context.outbox.claim(limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        batches = list(executor.map(lambda _: claim_one(), range(2)))

    claimed = [message for batch in batches for message in batch]
    assert len(claimed) == 2
    assert len({message.id for message in claimed}) == 2
    assert all(message.status == "processing" for message in claimed)
