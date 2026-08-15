from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cdek import (
    is_compatible_delivery_mode,
    millimetres_to_cdek_centimetres,
    rubles_to_kopecks,
)
from app.integrations import CdekTimeoutError, CdekUnavailableError
from app.repositories import Product, ProductPriceTier
from app.schemas import CdekDeliveryType


class FakeCdekClient:
    def __init__(self) -> None:
        self.city_items: list[Mapping[str, Any]] = []
        self.office_items: list[Mapping[str, Any]] = []
        self.tariffs: list[Mapping[str, Any]] = []
        self.tariff_payload: Mapping[str, Any] | None = None
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    def cities(self, **kwargs) -> list[Mapping[str, Any]]:
        self._maybe_raise()
        assert kwargs["request_id"]
        return self.city_items

    def delivery_points(self, **kwargs) -> list[Mapping[str, Any]]:
        self._maybe_raise()
        assert kwargs["request_id"]
        return self.office_items

    def tariff_list(
        self, payload: Mapping[str, Any], **kwargs
    ) -> list[Mapping[str, Any]]:
        self._maybe_raise()
        assert kwargs["request_id"]
        self.tariff_payload = payload
        return self.tariffs


def _add_adrosta_products(application: FastAPI) -> None:
    repository = application.state.context.products
    for sku, name in (
        ("opt-san-green", "SAN Green"),
        ("opt-san-blue", "SAN Blue"),
    ):
        repository.create(
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
        repository.replace_price_tiers(
            sku,
            (
                ProductPriceTier(1, 4, 32_000),
                ProductPriceTier(5, 9, 29_000),
                ProductPriceTier(10, None, 26_000),
            ),
        )


@pytest.fixture
def cdek_app(app_factory: Callable[..., FastAPI]):
    application = app_factory(CDEK_FROM_CITY_CODE=137)
    _add_adrosta_products(application)
    fake = FakeCdekClient()
    application.state.context.cdek_service.client = fake
    return application, fake


def _tariff(*, mode: int, amount: object = "1234.50") -> dict[str, object]:
    return {
        "tariff_code": 136,
        "tariff_name": "Посылка склад-склад",
        "tariff_description": "Тестовое описание",
        "delivery_mode": mode,
        "delivery_sum": amount,
        "period_min": 2,
        "period_max": 4,
        "calendar_min": 3,
        "ignored": "not exposed",
    }


def test_city_lookup_normalizes_and_does_not_proxy_raw_fields(cdek_app) -> None:
    application, fake = cdek_app
    fake.city_items = [
        {
            "code": 44,
            "city": "Москва",
            "region": "Москва",
            "country_code": "ru",
            "longitude": 37.6,
            "raw_secret": "not exposed",
        }
    ]
    with TestClient(application) as client:
        response = client.get(
            "/api/delivery/cdek/cities", params={"query": " Москва ", "countryCode": "ru"}
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"code": 44, "city": "Москва", "region": "Москва", "countryCode": "RU"}
        ]
    }


def test_empty_city_result_is_explicit_not_found(cdek_app) -> None:
    application, fake = cdek_app
    fake.city_items = []
    with TestClient(application) as client:
        response = client.get(
            "/api/delivery/cdek/cities", params={"query": "Несуществующий"}
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CDEK_LOCATION_NOT_FOUND"


def test_offices_return_only_pvz_and_compact_fields(cdek_app) -> None:
    application, fake = cdek_app
    fake.office_items = [
        {
            "code": "MSK123",
            "name": "ПВЗ",
            "type": "PVZ",
            "work_time": "Пн-Пт 10-20",
            "location": {
                "address": "Москва, ул. Тестовая, 1",
                "city_code": 44,
                "postal_code": "101000",
                "latitude": 55.75,
                "longitude": "37.61",
            },
            "phones": [{"number": "not exposed"}],
        },
        {
            "code": "POST1",
            "name": "Постамат",
            "type": "POSTOMAT",
            "location": {"address": "x", "city_code": 44},
        },
    ]
    with TestClient(application) as client:
        response = client.get("/api/delivery/cdek/offices", params={"cityCode": 44})

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "code": "MSK123",
                "name": "ПВЗ",
                "address": "Москва, ул. Тестовая, 1",
                "cityCode": 44,
                "postalCode": "101000",
                "latitude": "55.75",
                "longitude": "37.61",
                "workTime": "Пн-Пт 10-20",
                "type": "PVZ",
            }
        ]
    }


def test_quote_uses_five_canonical_packages_and_normalizes_tariff(cdek_app) -> None:
    application, fake = cdek_app
    fake.tariffs = [_tariff(mode=4), _tariff(mode=3, amount="999.00")]
    with TestClient(application) as client:
        response = client.post(
            "/api/delivery/cdek/quote",
            json={
                "deliveryType": "pickup",
                "toCityCode": 44,
                "items": [{"sku": "opt-san-green", "boxes": 5}],
            },
        )

    assert response.status_code == 200
    assert fake.tariff_payload is not None
    assert fake.tariff_payload["from_location"] == {"code": 137}
    assert fake.tariff_payload["to_location"] == {"code": 44}
    packages = fake.tariff_payload["packages"]
    assert isinstance(packages, list)
    assert len(packages) == 5
    assert packages == [
        {"number": str(number), "weight": 10_900, "length": 33, "width": 20, "height": 27}
        for number in range(1, 6)
    ]
    assert response.json() == {
        "deliveryType": "pickup",
        "fromCityCode": 137,
        "toCityCode": 44,
        "cargo": {"totalBoxes": 5, "totalWeightGrams": 54_500, "cargoPlaces": 5},
        "options": [
            {
                "tariffCode": 136,
                "tariffName": "Посылка склад-склад",
                "tariffDescription": "Тестовое описание",
                "deliveryMode": 4,
                "deliveryAmountKopecks": 123_450,
                "periodMinDays": 2,
                "periodMaxDays": 4,
            }
        ],
    }


def test_multi_sku_quote_preserves_each_physical_box(cdek_app) -> None:
    application, fake = cdek_app
    fake.tariffs = [_tariff(mode=4)]
    with TestClient(application) as client:
        response = client.post(
            "/api/delivery/cdek/quote",
            json={
                "deliveryType": "pickup",
                "toCityCode": 44,
                "items": [
                    {"sku": "opt-san-green", "boxes": 2},
                    {"sku": "opt-san-blue", "boxes": 3},
                ],
            },
        )

    assert response.status_code == 200
    assert fake.tariff_payload is not None
    packages = fake.tariff_payload["packages"]
    assert isinstance(packages, list)
    assert len(packages) == 5
    assert all(package["weight"] == 10_900 for package in packages)


def test_frontend_cannot_supply_physical_package_data(cdek_app) -> None:
    application, _ = cdek_app
    with TestClient(application) as client:
        response = client.post(
            "/api/delivery/cdek/quote",
            json={
                "deliveryType": "pickup",
                "toCityCode": 44,
                "items": [
                    {"sku": "opt-san-green", "boxes": 5, "weight": 1, "length": 1}
                ],
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (CdekUnavailableError("CDEK_UNAVAILABLE"), 503, "CDEK_UNAVAILABLE"),
        (CdekTimeoutError("CDEK_TIMEOUT"), 504, "CDEK_TIMEOUT"),
    ],
)
def test_cdek_failures_are_safe_api_errors(cdek_app, error, status_code, code) -> None:
    application, fake = cdek_app
    fake.error = error
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/delivery/cdek/cities", params={"query": "Москва"}
        )
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert "unit-test" not in response.text


def test_missing_credentials_returns_controlled_configuration_error(
    app: FastAPI,
) -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/delivery/cdek/cities", params={"query": "Москва"}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CDEK_NOT_CONFIGURED"


def test_no_compatible_tariff_is_explicit_error(cdek_app) -> None:
    application, fake = cdek_app
    fake.tariffs = [_tariff(mode=3)]
    with TestClient(application) as client:
        response = client.post(
            "/api/delivery/cdek/quote",
            json={
                "deliveryType": "pickup",
                "toCityCode": 44,
                "items": [{"sku": "opt-san-green", "boxes": 1}],
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CDEK_NO_TARIFFS"


@pytest.mark.parametrize(
    ("origin", "delivery_type", "expected_mode"),
    [
        ("door", CdekDeliveryType.DOOR, 1),
        ("door", CdekDeliveryType.PICKUP, 2),
        ("warehouse", CdekDeliveryType.DOOR, 3),
        ("warehouse", CdekDeliveryType.PICKUP, 4),
    ],
)
def test_official_delivery_mode_mapping(origin, delivery_type, expected_mode) -> None:
    assert is_compatible_delivery_mode(origin, delivery_type, expected_mode)
    assert not is_compatible_delivery_mode(origin, delivery_type, 99)


def test_dimension_and_money_conversion_are_exact() -> None:
    assert [millimetres_to_cdek_centimetres(value) for value in (330, 200, 265)] == [
        33,
        20,
        27,
    ]
    assert rubles_to_kopecks("1234.50") == 123_450
    assert rubles_to_kopecks(1234.5) == 123_450


def test_openapi_documents_typed_cdek_contract(cdek_app) -> None:
    application, _ = cdek_app
    schema = application.openapi()
    for path in (
        "/api/delivery/cdek/cities",
        "/api/delivery/cdek/offices",
        "/api/delivery/cdek/quote",
    ):
        assert path in schema["paths"]
    item_properties = schema["components"]["schemas"]["OrderItem"]["properties"]
    assert set(item_properties) == {"sku", "boxes"}
