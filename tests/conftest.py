from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repositories import Product


ALLOWED_ORIGIN = "https://shop.example.test"


def _seed_products(application: FastAPI) -> None:
    products = application.state.context.products
    products.create(
        Product(
            sku="ADR-001",
            name="ADROSTA test product 1",
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
            box_weight_grams=25_000,
            box_volume_mm3=240_000_000,
            box_length_mm=800,
            box_width_mm=500,
            box_height_mm=600,
        )
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
            "DATABASE_PATH": str(database_path),
            "CORS_ALLOWED_ORIGINS": ALLOWED_ORIGIN,
            "APP_HASH_SECRET": "test-only-hmac-secret-never-use-in-production",
            "ORDER_RATE_LIMIT_COUNT": "100",
            "ORDER_RATE_LIMIT_WINDOW_SECONDS": "60",
            "WEBHOOK_ENABLED": "false",
            "OUTBOX_ENABLED": "true",
            "ALLOW_STORE_ONLY": "true",
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
                "method": "transport_company",
                "transportCompany": "Тестовая транспортная компания",
                "region": "Московская область",
                "city": "Москва",
                "pickupPoint": "Тестовый пункт выдачи",
                "unloadingRequired": True,
                "accessRestrictions": "Въезд по пропуску",
                "recipient": {
                    "contactName": "Пётр Иванов",
                    "phone": "+7 999 765-43-21",
                    "email": "recipient@EXAMPLE.COM",
                },
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
