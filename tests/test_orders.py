from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from decimal import Decimal
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
    assert body["totals"]["boxes"] == 3
    assert body["totals"]["weightGrams"] == 50_000
    assert body["totals"]["volumeMm3"] == 480_000_000
    assert Decimal(str(body["totals"]["weightKg"])) == Decimal("50")
    assert Decimal(str(body["totals"]["volumeM3"])) == Decimal("0.48")
    assert body["items"][0] == {
        "sku": "ADR-001",
        "boxes": 2,
        "boxWeightGrams": 12_500,
        "boxVolumeMm3": 120_000_000,
        "lengthMm": 600,
        "widthMm": 400,
        "heightMm": 500,
        "weightGrams": 25_000,
        "volumeMm3": 240_000_000,
        "weightKg": "25",
        "volumeM3": "0.24",
    }

    with app.state.context.database.connection() as connection:
        stored = connection.execute(
            """
            SELECT buyer_type, buyer_phone, buyer_email,
                   recipient_phone, recipient_email
            FROM orders WHERE id = ?
            """,
            (body["orderId"],),
        ).fetchone()
    assert stored is not None
    assert stored["buyer_type"] == "individual"
    assert stored["buyer_phone"] == "+79991234567"
    assert stored["buyer_email"] == "buyer@example.com"
    assert stored["recipient_phone"] == "+79997654321"
    assert stored["recipient_email"] == "recipient@example.com"


def test_creates_legal_order_and_persists_required_company(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(legal=True)

    response = _post_order(client, payload, key="legal-order-key-0001")

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
        "buyer_type": "legal",
        "company_name": "ООО Тест",
        "company_inn": "7707083893",
        "company_kpp": "773601001",
        "company_legal_address": "г. Москва, тестовый адрес, д. 1",
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


def test_rejects_legal_buyer_without_company(
    client: TestClient,
    app: FastAPI,
    order_payload_factory: OrderPayloadFactory,
) -> None:
    payload = order_payload_factory(legal=True)
    del payload["company"]

    response = _post_order(client, payload)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["field"] == "company"
    assert response.json()["error"]["details"][0]["code"] == "REQUIRED_COMPANY"
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


@pytest.mark.parametrize(
    ("section", "field", "expected_path"),
    [
        ("buyer", "contactName", "buyer.contactName"),
        ("delivery", "region", "delivery.region"),
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
