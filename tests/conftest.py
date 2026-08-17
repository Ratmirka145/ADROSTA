from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

# app.main exposes a module-level ASGI app, so collection needs an explicit
# isolated test URL before importing it.
os.environ.setdefault("APP_ENV", "test")
os.environ["DEBUG"] = "false"
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.config import Settings
from app.main import create_app
from app.models import Base
from app.repositories import Product, ProductPriceTier


ALLOWED_ORIGIN = "https://shop.example.test"


def _seed_products(application: FastAPI) -> None:
    products = application.state.context.products
    products.create(
        Product(
            sku="ADR-001",
            name="ADROSTA test product 1",
            units_per_box=10,
            box_weight_grams=12_500,
            box_volume_mm3=120_000_000,
            box_length_mm=600,
            box_width_mm=400,
            box_height_mm=500,
        )
    )
    products.create(
        Product(
            sku="ADR-002",
            name="ADROSTA test product 2",
            units_per_box=20,
            box_weight_grams=25_000,
            box_volume_mm3=240_000_000,
            box_length_mm=800,
            box_width_mm=500,
            box_height_mm=600,
        )
    )
    products.replace_price_tiers(
        "ADR-001",
        (ProductPriceTier(1, None, 10_000),),
    )
    products.replace_price_tiers(
        "ADR-002",
        (ProductPriceTier(1, None, 20_000),),
    )


@pytest.fixture
def allowed_origin() -> str:
    return ALLOWED_ORIGIN


@pytest.fixture
def settings_factory(
    tmp_path: Path,
) -> Callable[..., Settings]:
    def make_settings(**overrides: object) -> Settings:
        database_path = tmp_path / f"adrosta-{uuid4().hex}.sqlite3"
        environ: dict[str, str] = {
            "APP_ENV": "test",
            "DEBUG": "false",
            "API_DOCS_ENABLED": "false",
            "LOG_LEVEL": "WARNING",
            "DATABASE_URL": f"sqlite+pysqlite:///{database_path}",
            "CORS_ALLOWED_ORIGINS": ALLOWED_ORIGIN,
            "APP_HASH_SECRET": "test-only-hmac-secret-never-use-in-production",
            "ORDER_RATE_LIMIT_COUNT": "100",
            "ORDER_RATE_LIMIT_WINDOW_SECONDS": "60",
            "WEBHOOK_ENABLED": "false",
            "OUTBOX_ENABLED": "true",
            "ALLOW_STORE_ONLY": "true",
            "SELLER_LEGAL_NAME": "ООО АДРОСТА ТЕСТ",
            "SELLER_INN": "7707083893",
            "SELLER_KPP": "773601001",
            "SELLER_LEGAL_ADDRESS": "г. Москва, тестовый адрес, д. 10",
            "SELLER_BANK_NAME": "Тестовый банк",
            "SELLER_BIK": "044525000",
            "SELLER_CHECKING_ACCOUNT": "40702810000000000001",
            "SELLER_CORRESPONDENT_ACCOUNT": "30101810000000000000",
            "SELLER_PHONE": "+79990000000",
            "SELLER_EMAIL": "seller@example.test",
            "INVOICE_TAX_TEXT": "Без НДС (тест)",
            "INVOICE_PAYMENT_PURPOSE_TEMPLATE": (
                "Оплата по счёту {invoice_number} от {invoice_date}"
            ),
        }
        for name, value in overrides.items():
            if isinstance(value, bool):
                environ[name] = "true" if value else "false"
            else:
                environ[name] = str(value)
        return Settings.from_env(environ, load_env_file=False)

    return make_settings


@pytest.fixture
def app_factory(
    settings_factory: Callable[..., Settings],
) -> Iterator[Callable[..., FastAPI]]:
    applications: list[FastAPI] = []

    def make_app(*, seed_products: bool = True, **settings_overrides: object) -> FastAPI:
        application = create_app(settings_factory(**settings_overrides))
        engine = application.state.context.database.engine

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(
            dbapi_connection: object, connection_record: object
        ) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        Base.metadata.create_all(engine)
        if seed_products:
            _seed_products(application)
        applications.append(application)
        return application

    yield make_app

    for application in applications:
        application.state.context.close()


@pytest.fixture
def app(app_factory: Callable[..., FastAPI]) -> FastAPI:
    return app_factory()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def order_payload_factory() -> Callable[..., dict[str, object]]:
    def make_payload(*, business: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "buyer": {
                "type": "business" if business else "individual",
                "contactName": "Иван Петров",
                "phone": "8 (999) 123-45-67",
                "email": "buyer@EXAMPLE.COM",
            },
            "delivery": {
                "method": "self_pickup",
            },
            "comment": "Тестовый заказ",
            "items": [
                {"sku": "ADR-001", "boxes": 2},
                {"sku": "ADR-002", "boxes": 1},
            ],
        }
        if business:
            payload["company"] = {
                "name": "ООО Тест",
                "inn": "7707083893",
                "kpp": "773601001",
                "legalAddress": "г. Москва, тестовый адрес, д. 1",
            }
        return payload

    return make_payload
