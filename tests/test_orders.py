from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import OrderModel
from app.integrations import CdekTimeoutError, CdekUnavailableError


OrderPayloadFactory = Callable[..., dict[str, object]]


class FakeOrderCdekClient:
    def __init__(self) -> None:
        self.amount = "1234.50"
        self.tariff_code = 136
        self.tariff_calls = 0
        self.office_calls = 0
        self.error: Exception | None = None
        self.office_city_code = 44
        self.office_code = "MSK123"

    def tariff_list(
        self, payload: Mapping[str, Any], **kwargs: object
    ) -> list[Mapping[str, Any]]:
        assert kwargs["request_id"]
        self.tariff_calls += 1
        if self.error is not None:
            raise self.error
        assert payload["to_location"] == {"code": 44}
        return [
            {
                "tariff_code": self.tariff_code,
                "tariff_name": "Посылка склад-склад",
                "tariff_description": "Тестовый тариф",
                "delivery_mode": delivery_mode,
                "delivery_sum": self.amount,
                "period_min": 2,
                "period_max": 4,
            }
            for delivery_mode in (3, 4)
        ]

    def delivery_points(self, **kwargs: object) -> list[Mapping[str, Any]]:
        assert kwargs["request_id"]
        self.office_calls += 1
        if self.error is not None:
            raise self.error
        return [
            {
                "code": self.office_code,
                "name": "ПВЗ Тест",
                "type": "PVZ",
                "location": {
                    "address": "Москва, ул. Тестовая, 1",
                    "city_code": self.office_city_code,
                },
            }
        ]


@pytest.fixture
def order_cdek_client(app: FastAPI) -> FakeOrderCdekClient:
    fake = FakeOrderCdekClient()
    service = app.state.context.cdek_service
    service.client = fake
    service.from_city_code = 137
    return fake


def _post_order(
    client: TestClient,
    payload: dict[str, object],
    *,
    key: str = "test-order-key-0001",
):
    return client.post(
        "/api/orders",
        json=payload,
        headers={"Idempotency-Key": key},
    )


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def _order_count(application: FastAPI) -> int:
    with application.state.context.database.session() as session:
        return int(session.scalar(select(func.count()).select_from(OrderModel)) or 0)


def _stored_order(
    application: FastAPI, order_id: str, *field_names: str
) -> dict[str, object]:
    with application.state.context.database.session() as session:
        order = session.get(OrderModel, order_id)
        assert order is not None
        return {name: getattr(order, name) for name in field_names}


def _pickup_delivery(*, tariff_code: int = 136) -> dict[str, object]:
    return {
        "method": "cdek",
        "type": "pickup",
        "toCityCode": 44,
        "tariffCode": tariff_code,
        "officeCode": "MSK123",
    }


def test_creates_individual_order_and_recalculates_trusted_totals(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()

    response = _post_order(client, payload, key="individual-order-0001")

    assert response.status_code == 201
    assert response.headers["idempotency-replayed"] == "false"
    body = response.json()
    UUID(body["orderId"])
    assert body["status"] == "accepted"
    assert body["integrationStatus"] == "stored"
    assert body["replayed"] is False
    assert body["items"][0] == {
        "sku": "ADR-001",
        "name": "ADROSTA test product 1",
        "boxes": 2,
        "unitsPerBox": 10,
        "units": 20,
        "pricePerUnitKopecks": 10_000,
        "pricePerBoxKopecks": 100_000,
        "lineAmountKopecks": 200_000,
        "weightPerBoxGrams": 12_500,
        "totalWeightGrams": 25_000,
        "boxVolumeMm3": 120_000_000,
        "lengthMm": 600,
        "widthMm": 400,
        "heightMm": 500,
        "cargoPlaces": 2,
        "totalVolumeMm3": 240_000_000,
    }
    assert body["totals"] == {
        "totalBoxes": 3,
        "totalUnits": 40,
        "productsAmountKopecks": 600_000,
        "deliveryAmountKopecks": 0,
        "grandTotalKopecks": 600_000,
        "totalWeightGrams": 50_000,
        "cargoPlaces": 3,
        "totalVolumeMm3": 480_000_000,
    }

    stored = _stored_order(
        app, body["orderId"], "buyer_type", "buyer_phone", "buyer_email"
    )
    assert stored["buyer_type"] == "individual"
    assert stored["buyer_phone"] == "+79991234567"
    assert stored["buyer_email"] == "buyer@example.com"


def test_creates_self_pickup_order_and_persists_only_delivery_method(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {"method": "self_pickup"}

    response = _post_order(client, payload, key="self-pickup-order-key-0001")

    assert response.status_code == 201
    assert response.json()["delivery"]["method"] == "self_pickup"
    assert response.json()["totals"]["deliveryAmountKopecks"] == 0
    assert response.json()["totals"]["grandTotalKopecks"] == 600_000
    assert order_cdek_client.tariff_calls == 0
    assert order_cdek_client.office_calls == 0
    stored = _stored_order(
        app,
        response.json()["orderId"],
        "delivery_method", "delivery_type", "delivery_region", "delivery_city",
        "delivery_office_code", "delivery_postcode", "delivery_street",
        "delivery_house", "delivery_apartment", "delivery_amount_kopecks",
        "grand_total_kopecks", "cdek_tariff_code",
    )
    assert stored == {
        "delivery_method": "self_pickup",
        "delivery_type": None,
        "delivery_region": None,
        "delivery_city": None,
        "delivery_office_code": None,
        "delivery_postcode": None,
        "delivery_street": None,
        "delivery_house": None,
        "delivery_apartment": None,
        "delivery_amount_kopecks": 0,
        "grand_total_kopecks": 600_000,
        "cdek_tariff_code": None,
    }


def test_creates_cdek_pickup_order_and_persists_delivery_fields(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "pickup",
        "toCityCode": 44,
        "tariffCode": 136,
        "officeCode": "MSK123",
    }

    response = _post_order(client, payload, key="cdek-pickup-order-key-0001")

    assert response.status_code == 201
    body = response.json()
    assert body["totals"]["deliveryAmountKopecks"] == 123_450
    assert body["totals"]["grandTotalKopecks"] == 723_450
    assert body["delivery"] == {
        "method": "cdek",
        "type": "pickup",
        "toCityCode": 44,
        "tariffCode": 136,
        "tariffName": "Посылка склад-склад",
        "deliveryMode": 4,
        "officeCode": "MSK123",
        "region": None,
        "city": None,
        "postcode": None,
        "street": None,
        "house": None,
        "apartment": None,
        "periodMinDays": 2,
        "periodMaxDays": 4,
    }
    assert order_cdek_client.tariff_calls == 1
    assert order_cdek_client.office_calls == 1
    stored = _stored_order(
        app,
        response.json()["orderId"],
        "delivery_method", "delivery_type", "delivery_region", "delivery_city",
        "delivery_office_code", "cdek_to_city_code", "cdek_tariff_code",
        "cdek_tariff_name", "cdek_delivery_mode", "cdek_period_min_days",
        "cdek_period_max_days", "delivery_amount_kopecks", "grand_total_kopecks",
    )
    assert stored == {
        "delivery_method": "cdek",
        "delivery_type": "pickup",
        "delivery_region": None,
        "delivery_city": None,
        "delivery_office_code": "MSK123",
        "cdek_to_city_code": 44,
        "cdek_tariff_code": 136,
        "cdek_tariff_name": "Посылка склад-склад",
        "cdek_delivery_mode": 4,
        "cdek_period_min_days": 2,
        "cdek_period_max_days": 4,
        "delivery_amount_kopecks": 123_450,
        "grand_total_kopecks": 723_450,
    }


def test_creates_cdek_door_order_and_persists_structured_address(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "door",
        "toCityCode": 44,
        "tariffCode": 136,
        "region": "Москва",
        "city": "Москва",
        "postcode": "115054",
        "street": "Дубининская",
        "house": "53",
        "apartment": "12",
    }

    response = _post_order(client, payload, key="cdek-door-order-key-0001")

    assert response.status_code == 201
    body = response.json()
    assert body["totals"]["deliveryAmountKopecks"] == 123_450
    assert body["totals"]["grandTotalKopecks"] == 723_450
    assert body["delivery"]["deliveryMode"] == 3
    assert body["delivery"]["tariffName"] == "Посылка склад-склад"
    assert order_cdek_client.tariff_calls == 1
    assert order_cdek_client.office_calls == 0
    stored = _stored_order(
        app,
        response.json()["orderId"],
        "delivery_method", "delivery_type", "delivery_region", "delivery_city",
        "delivery_postcode", "delivery_street", "delivery_house",
        "delivery_apartment", "delivery_office_code", "cdek_to_city_code",
        "cdek_tariff_code", "cdek_tariff_name", "cdek_delivery_mode",
        "cdek_period_min_days", "cdek_period_max_days",
        "delivery_amount_kopecks", "grand_total_kopecks",
    )
    assert stored == {
        "delivery_method": "cdek",
        "delivery_type": "door",
        "delivery_region": "Москва",
        "delivery_city": "Москва",
        "delivery_postcode": "115054",
        "delivery_street": "Дубининская",
        "delivery_house": "53",
        "delivery_apartment": "12",
        "delivery_office_code": None,
        "cdek_to_city_code": 44,
        "cdek_tariff_code": 136,
        "cdek_tariff_name": "Посылка склад-склад",
        "cdek_delivery_mode": 3,
        "cdek_period_min_days": 2,
        "cdek_period_max_days": 4,
        "delivery_amount_kopecks": 123_450,
        "grand_total_kopecks": 723_450,
    }


def test_final_cdek_price_replaces_preview_and_uses_integer_grand_total(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    order_cdek_client.amount = "1000.00"
    preview = client.post(
        "/api/delivery/cdek/quote",
        json={
            "deliveryType": "pickup",
            "toCityCode": 44,
            "items": [
                {"sku": "ADR-001", "boxes": 2},
                {"sku": "ADR-002", "boxes": 1},
            ],
        },
    )
    assert preview.status_code == 200
    assert preview.json()["options"][0]["deliveryAmountKopecks"] == 100_000

    order_cdek_client.amount = "1234.50"
    payload = order_payload_factory()
    payload["delivery"] = _pickup_delivery()
    response = _post_order(client, payload, key="changed-cdek-price-key-0001")

    assert response.status_code == 201
    assert response.json()["totals"]["deliveryAmountKopecks"] == 123_450
    assert response.json()["totals"]["grandTotalKopecks"] == 723_450
    stored = _stored_order(
        app,
        response.json()["orderId"],
        "products_amount_kopecks",
        "delivery_amount_kopecks",
        "grand_total_kopecks",
    )
    assert stored == {
        "products_amount_kopecks": 600_000,
        "delivery_amount_kopecks": 123_450,
        "grand_total_kopecks": 723_450,
    }


def test_idempotent_cdek_replay_does_not_call_upstream_twice(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = _pickup_delivery()

    first = _post_order(client, payload, key="idempotent-cdek-key-0001")
    replay = _post_order(client, payload, key="idempotent-cdek-key-0001")

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["orderId"] == first.json()["orderId"]
    assert replay.json()["replayed"] is True
    assert replay.json()["totals"] == first.json()["totals"]
    assert order_cdek_client.tariff_calls == 1
    assert order_cdek_client.office_calls == 1
    assert _order_count(app) == 1


def test_unavailable_selected_tariff_does_not_create_order(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = _pickup_delivery(tariff_code=999)

    response = _post_order(client, payload, key="missing-cdek-tariff-key-0001")

    assert response.status_code == 422
    assert _error_code(response) == "CDEK_TARIFF_UNAVAILABLE"
    assert order_cdek_client.office_calls == 0
    assert _order_count(app) == 0


def test_office_must_belong_to_selected_cdek_city(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    order_cdek_client.office_city_code = 77
    payload = order_payload_factory()
    payload["delivery"] = _pickup_delivery()

    response = _post_order(client, payload, key="wrong-cdek-office-key-0001")

    assert response.status_code == 422
    assert _error_code(response) == "CDEK_OFFICE_UNAVAILABLE"
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    ("integration_error", "expected_status", "expected_code"),
    (
        (CdekTimeoutError("CDEK_TIMEOUT"), 504, "CDEK_TIMEOUT"),
        (CdekUnavailableError("CDEK_UNAVAILABLE"), 503, "CDEK_UNAVAILABLE"),
    ),
)
def test_cdek_failure_during_final_verification_does_not_create_order(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
    integration_error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    order_cdek_client.error = integration_error
    payload = order_payload_factory()
    payload["delivery"] = _pickup_delivery()

    response = _post_order(client, payload, key="timeout-cdek-order-key-0001")

    assert response.status_code == expected_status
    assert _error_code(response) == expected_code
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    "price_field",
    ("deliveryAmount", "deliveryAmountKopecks", "deliveryPrice", "price"),
)
def test_frontend_cannot_override_delivery_price(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
    price_field: str,
) -> None:
    payload = order_payload_factory()
    delivery = _pickup_delivery()
    delivery[price_field] = 1
    payload["delivery"] = delivery

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert any(
        detail.get("field") == f"delivery.{price_field}"
        and detail.get("code") == "UNKNOWN_FIELD"
        for detail in response.json()["error"]["details"]
    )
    assert order_cdek_client.tariff_calls == 0
    assert _order_count(app) == 0


def test_frontend_cannot_override_cdek_origin_city(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    order_cdek_client: FakeOrderCdekClient,
) -> None:
    payload = order_payload_factory()
    delivery = _pickup_delivery()
    delivery["fromCityCode"] = 999
    payload["delivery"] = delivery

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert any(
        detail.get("field") == "delivery.fromCityCode"
        and detail.get("code") == "UNKNOWN_FIELD"
        for detail in response.json()["error"]["details"]
    )
    assert order_cdek_client.tariff_calls == 0
    assert _order_count(app) == 0


def test_openapi_exposes_order_delivery_selection_and_commercial_totals(
    app: FastAPI,
) -> None:
    schemas = app.openapi()["components"]["schemas"]
    delivery_properties = schemas["Delivery"]["properties"]
    assert "toCityCode" in delivery_properties
    assert "tariffCode" in delivery_properties
    assert not {
        "fromCityCode",
        "deliveryAmount",
        "deliveryAmountKopecks",
        "deliveryPrice",
        "price",
    } & set(delivery_properties)
    assert {
        "productsAmountKopecks",
        "deliveryAmountKopecks",
        "grandTotalKopecks",
    } <= set(schemas["OrderCommercialTotalsResponse"]["properties"])


def test_rejects_cdek_without_delivery_type(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {"method": "cdek", "city": "Москва"}

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "REQUIRED_CDEK_TYPE"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    ("removed_field", "expected_code"),
    (
        ("toCityCode", "REQUIRED_CDEK_CITY_CODE"),
        ("tariffCode", "REQUIRED_CDEK_TARIFF"),
    ),
)
def test_rejects_cdek_without_server_verification_identifiers(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    removed_field: str,
    expected_code: str,
) -> None:
    payload = order_payload_factory()
    delivery = _pickup_delivery()
    delivery.pop(removed_field)
    payload["delivery"] = delivery

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert any(
        detail.get("code") == expected_code
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_cdek_pickup_without_office(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "pickup",
        "toCityCode": 44,
        "tariffCode": 136,
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "REQUIRED_CDEK_OFFICE"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_cdek_door_without_street(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "door",
        "toCityCode": 44,
        "tariffCode": 136,
        "city": "Москва",
        "house": "53",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "REQUIRED_STREET"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_cdek_door_without_house(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "door",
        "toCityCode": 44,
        "tariffCode": 136,
        "city": "Москва",
        "street": "Дубининская",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "REQUIRED_HOUSE"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_office_code_for_cdek_door_delivery(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "door",
        "toCityCode": 44,
        "tariffCode": 136,
        "city": "Москва",
        "street": "Дубининская",
        "house": "53",
        "officeCode": "MSK123",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "OFFICE_CODE_NOT_ALLOWED"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_cdek_fields_for_self_pickup(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {"method": "self_pickup", "city": "Москва"}

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "DELIVERY_FIELDS_NOT_ALLOWED"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_door_address_fields_for_cdek_pickup(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "pickup",
        "toCityCode": 44,
        "tariffCode": 136,
        "officeCode": "MSK123",
        "city": "Москва",
        "street": "Дубининская",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "DOOR_FIELDS_NOT_ALLOWED"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    "delivery",
    [
        {"method": "transport", "transportCompany": "СДЭК"},
        {"method": "pek"},
        {"method": "ozon"},
    ],
)
def test_rejects_obsolete_or_arbitrary_delivery_methods(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    delivery: dict[str, object],
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = delivery

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("field") == "delivery.method"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_creates_business_order_and_persists_required_company(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(business=True)

    response = _post_order(client, payload, key="business-order-key-0001")

    assert response.status_code == 201
    order_id = response.json()["orderId"]
    stored = _stored_order(
        app, order_id, "buyer_type", "company_name", "company_inn",
        "company_kpp", "company_legal_address",
    )
    assert stored == {
        "buyer_type": "business",
        "company_name": "ООО Тест",
        "company_inn": "7707083893",
        "company_kpp": "773601001",
        "company_legal_address": "г. Москва, тестовый адрес, д. 1",
    }


def test_creates_business_order_for_individual_entrepreneur_without_kpp(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(business=True)
    payload["company"] = {
        "name": "ИП Тест",
        "inn": "123456789012",
        "kpp": None,
        "legalAddress": "г. Москва, тестовый адрес, д. 2",
    }

    response = _post_order(client, payload, key="entrepreneur-order-key-0001")

    assert response.status_code == 201
    order_id = response.json()["orderId"]
    stored = _stored_order(
        app, order_id, "buyer_type", "company_inn", "company_kpp"
    )
    assert stored == {
        "buyer_type": "business",
        "company_inn": "123456789012",
        "company_kpp": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phone", "12345"),
        ("phone", "+8 (999) 123-45-67"),
        ("email", "not-an-email"),
    ],
)
def test_rejects_invalid_buyer_phone_and_email(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    field: str,
    value: str,
) -> None:
    payload = order_payload_factory()
    payload["buyer"][field] = value  # type: ignore[index]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("field") == f"buyer.{field}"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_business_buyer_without_company(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(business=True)
    del payload["company"]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["field"] == "company"
    assert response.json()["error"]["details"][0]["code"] == "REQUIRED_COMPANY"
    assert _order_count(app) == 0


def test_rejects_organization_without_kpp(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(business=True)
    payload["company"] = {
        "name": "ООО Без КПП",
        "inn": "7707083893",
        "kpp": None,
        "legalAddress": "г. Москва, тестовый адрес, д. 3",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("field") == "company.kpp"
        and detail.get("code") == "REQUIRED_KPP"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_individual_entrepreneur_with_kpp(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(business=True)
    payload["company"] = {
        "name": "ИП с КПП",
        "inn": "123456789012",
        "kpp": "773601001",
        "legalAddress": "г. Москва, тестовый адрес, д. 4",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("field") == "company.kpp"
        and detail.get("code") == "KPP_NOT_ALLOWED"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_rejects_company_for_individual_buyer(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["company"] = {
        "name": "ООО Лишнее",
        "inn": "7707083893",
        "kpp": "773601001",
        "legalAddress": "г. Москва, тестовый адрес, д. 1",
    }

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["field"] == "company"
    assert response.json()["error"]["details"][0]["code"] == "COMPANY_NOT_ALLOWED"
    assert _order_count(app) == 0


def test_rejects_obsolete_legal_buyer_type(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["buyer"]["type"] = "legal"  # type: ignore[index]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("field") == "buyer.type"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    ("section", "field", "expected_path"),
    [
        ("buyer", "contactName", "buyer.contactName"),
        ("delivery", "method", "delivery.method"),
    ],
)
def test_rejects_missing_required_fields(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    section: str,
    field: str,
    expected_path: str,
) -> None:
    payload = order_payload_factory()
    del payload[section][field]  # type: ignore[index]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert {
        "field": expected_path,
        "code": "REQUIRED_FIELD",
    }.items() <= response.json()["error"]["details"][0].items()
    assert _order_count(app) == 0


def test_rejects_unknown_sku(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["items"] = [{"sku": "UNKNOWN-SKU", "boxes": 1}]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "UNKNOWN_SKU"
    assert "UNKNOWN-SKU" not in response.text
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("order", "totals", {"weightKg": 1, "volumeM3": 1}),
        ("item", "weightGrams", 1),
        ("item", "volumeMm3", 1),
        ("item", "dimensions", {"length": 1, "width": 1, "height": 1}),
    ],
)
def test_rejects_client_supplied_cargo_metrics(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
    target: str,
    field: str,
    value: object,
) -> None:
    payload = order_payload_factory()
    if target == "order":
        payload[field] = value
    else:
        payload["items"][0][field] = value  # type: ignore[index]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail["code"] == "UNKNOWN_FIELD"
        for detail in response.json()["error"]["details"]
    )
    assert _order_count(app) == 0


def test_same_idempotency_key_and_payload_replays_original_order(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()

    first = _post_order(client, payload, key="replay-order-key-0001")
    second = _post_order(client, payload, key="replay-order-key-0001")

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["idempotency-replayed"] == "true"
    assert second.json()["replayed"] is True
    assert second.json()["orderId"] == first.json()["orderId"]
    assert _order_count(app) == 1


def test_same_idempotency_key_with_changed_payload_is_a_conflict(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    original = order_payload_factory()
    changed = deepcopy(original)
    changed["comment"] = "Другой заказ"

    first = _post_order(client, original, key="conflict-order-key-0001")
    second = _post_order(client, changed, key="conflict-order-key-0001")

    assert first.status_code == 201
    assert second.status_code == 409
    assert _error_code(second) == "IDEMPOTENCY_CONFLICT"
    assert _order_count(app) == 1


def test_duplicate_payload_with_different_key_is_rejected(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()

    first = _post_order(client, payload, key="duplicate-order-key-0001")
    second = _post_order(client, payload, key="duplicate-order-key-0002")

    assert first.status_code == 201
    assert second.status_code == 409
    assert _error_code(second) == "DUPLICATE_ORDER"
    assert _order_count(app) == 1


def test_duplicate_detection_is_independent_of_item_order(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    original = order_payload_factory()
    reordered = deepcopy(original)
    reordered["items"] = list(reversed(reordered["items"]))

    first = _post_order(client, original, key="ordered-items-key-0001")
    second = _post_order(client, reordered, key="ordered-items-key-0002")

    assert first.status_code == 201
    assert second.status_code == 409
    assert _error_code(second) == "DUPLICATE_ORDER"
    assert _order_count(app) == 1


def test_requires_valid_idempotency_key(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    response = client.post("/api/orders", json=order_payload_factory())

    assert response.status_code == 400
    assert _error_code(response) == "IDEMPOTENCY_KEY_REQUIRED"
    assert _order_count(app) == 0


def test_rate_limit_rejects_requests_above_configured_window(
    app_factory: Callable[..., FastAPI],
    order_payload_factory: OrderPayloadFactory,
) -> None:
    application = app_factory(ORDER_RATE_LIMIT_COUNT=2)

    with TestClient(application) as client:
        first_payload = order_payload_factory()
        first_payload["comment"] = "Заказ 1"
        second_payload = order_payload_factory()
        second_payload["comment"] = "Заказ 2"
        third_payload = order_payload_factory()
        third_payload["comment"] = "Заказ 3"

        first = _post_order(client, first_payload, key="rate-order-key-0001")
        second = _post_order(client, second_payload, key="rate-order-key-0002")
        third = _post_order(client, third_payload, key="rate-order-key-0003")

    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429
    assert _error_code(third) == "RATE_LIMITED"
    assert int(third.headers["retry-after"]) >= 1
    assert _order_count(application) == 2
