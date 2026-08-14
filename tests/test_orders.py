from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


OrderPayloadFactory = Callable[..., dict[str, object]]


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
    with application.state.context.database.connection() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0])


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
        "totalWeightGrams": 50_000,
        "cargoPlaces": 3,
        "totalVolumeMm3": 480_000_000,
    }

    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT buyer_type, buyer_phone, buyer_email
            FROM orders WHERE id = ?
            """,
            (body["orderId"],),
        ).fetchone()
    assert stored is not None
    assert stored["buyer_type"] == "individual"
    assert stored["buyer_phone"] == "+79991234567"
    assert stored["buyer_email"] == "buyer@example.com"


def test_creates_self_pickup_order_and_persists_only_delivery_method(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {"method": "self_pickup"}

    response = _post_order(client, payload, key="self-pickup-order-key-0001")

    assert response.status_code == 201
    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT delivery_method, delivery_type, delivery_region, delivery_city,
                   delivery_office_code, delivery_postcode, delivery_street,
                   delivery_house, delivery_apartment
            FROM orders WHERE id = ?
            """,
            (response.json()["orderId"],),
        ).fetchone()
    assert stored is not None
    assert dict(stored) == {
        "delivery_method": "self_pickup",
        "delivery_type": None,
        "delivery_region": None,
        "delivery_city": None,
        "delivery_office_code": None,
        "delivery_postcode": None,
        "delivery_street": None,
        "delivery_house": None,
        "delivery_apartment": None,
    }


def test_creates_cdek_pickup_order_and_persists_delivery_fields(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "pickup",
        "region": "Москва",
        "city": "Москва",
        "officeCode": None,
    }

    response = _post_order(client, payload, key="cdek-pickup-order-key-0001")

    assert response.status_code == 201
    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT delivery_method, delivery_type, delivery_region, delivery_city,
                   delivery_office_code
            FROM orders WHERE id = ?
            """,
            (response.json()["orderId"],),
        ).fetchone()
    assert stored is not None
    assert dict(stored) == {
        "delivery_method": "cdek",
        "delivery_type": "pickup",
        "delivery_region": "Москва",
        "delivery_city": "Москва",
        "delivery_office_code": None,
    }


def test_creates_cdek_door_order_and_persists_structured_address(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {
        "method": "cdek",
        "type": "door",
        "region": "Москва",
        "city": "Москва",
        "postcode": "115054",
        "street": "Дубининская",
        "house": "53",
        "apartment": "12",
    }

    response = _post_order(client, payload, key="cdek-door-order-key-0001")

    assert response.status_code == 201
    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT delivery_method, delivery_type, delivery_region, delivery_city,
                   delivery_postcode, delivery_street, delivery_house,
                   delivery_apartment, delivery_office_code
            FROM orders WHERE id = ?
            """,
            (response.json()["orderId"],),
        ).fetchone()
    assert stored is not None
    assert dict(stored) == {
        "delivery_method": "cdek",
        "delivery_type": "door",
        "delivery_region": "Москва",
        "delivery_city": "Москва",
        "delivery_postcode": "115054",
        "delivery_street": "Дубининская",
        "delivery_house": "53",
        "delivery_apartment": "12",
        "delivery_office_code": None,
    }


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


def test_rejects_cdek_pickup_without_city(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory()
    payload["delivery"] = {"method": "cdek", "type": "pickup"}

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert any(
        detail.get("code") == "REQUIRED_CITY"
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
    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT buyer_type, company_name, company_inn, company_kpp,
                   company_legal_address
            FROM orders WHERE id = ?
            """,
            (order_id,),
        ).fetchone()
    assert stored is not None
    assert dict(stored) == {
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
    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT buyer_type, company_inn, company_kpp
            FROM orders WHERE id = ?
            """,
            (order_id,),
        ).fetchone()
    assert stored is not None
    assert dict(stored) == {
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
