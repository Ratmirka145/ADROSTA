from __future__ import annotations

import hashlib
import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    CustomerSessionModel,
    CustomerSessionOrderModel,
    IdempotencyRecordModel,
    InvoiceModel,
    OrderModel,
)


COOKIE_NAME = "adrosta_customer_session"


def _create_order(
    client: TestClient,
    payload: dict[str, object],
    *,
    key: str,
):
    return client.post(
        "/api/orders",
        json=payload,
        headers={"Idempotency-Key": key},
    )


def _error_signature(response) -> tuple[int, str, str, tuple]:
    error = response.json()["error"]
    return (
        response.status_code,
        error["code"],
        error["message"],
        tuple(error["details"]),
    )


def test_order_creation_sets_secure_session_cookie_and_stores_only_hash(
    app_factory,
    order_payload_factory,
) -> None:
    application = app_factory(CUSTOMER_SESSION_COOKIE_SECURE=True)
    with TestClient(application, base_url="https://api.example.test") as client:
        response = _create_order(
            client,
            order_payload_factory(),
            key="customer-cookie-key-0001",
        )
        token = client.cookies.get(COOKIE_NAME)

    assert response.status_code == 201
    assert token is not None
    assert len(token) >= 43
    assert response.json()["orderNumber"].startswith("AD-")
    assert response.json()["orderPageUrl"] == (
        f'/order?number={response.json()["orderNumber"]}'
    )
    assert token not in response.text

    set_cookie = response.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/api" in set_cookie
    assert "Max-Age=7776000" in set_cookie
    assert "expires=" in set_cookie.casefold()
    assert "Domain=" not in set_cookie

    with application.state.context.database.session() as session:
        stored = session.scalar(select(CustomerSessionModel))
        assert stored is not None
        assert stored.token_hash != token
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert len(stored.token_hash) == 64
        assert session.scalar(
            select(func.count()).select_from(CustomerSessionOrderModel)
        ) == 1


def test_owner_can_read_customer_safe_order(
    client: TestClient,
    order_payload_factory,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-read-key-0001",
    )
    order_number = created.json()["orderNumber"]

    response = client.get(f"/api/customer/orders/{order_number}")

    assert response.status_code == 200
    assert response.json() == {
        "orderNumber": order_number,
        "status": "accepted",
        "createdAt": response.json()["createdAt"],
        "items": [
            {
                "sku": "ADR-001",
                "name": "ADROSTA test product 1",
                "quantity": 20,
                "unit": "шт.",
                "lineAmountKopecks": 200_000,
            },
            {
                "sku": "ADR-002",
                "name": "ADROSTA test product 2",
                "quantity": 20,
                "unit": "шт.",
                "lineAmountKopecks": 400_000,
            },
        ],
        "totals": {
            "productsAmountKopecks": 600_000,
            "deliveryAmountKopecks": 0,
            "grandTotalKopecks": 600_000,
        },
        "delivery": {
            "method": "self_pickup",
            "type": None,
            "description": "Самовывоз",
            "city": None,
            "officeCode": None,
            "tariffName": None,
            "periodMinDays": None,
            "periodMaxDays": None,
        },
        "invoice": {
            "number": response.json()["invoice"]["number"],
            "issuedAt": response.json()["invoice"]["issuedAt"],
            "pdfAvailable": True,
        },
    }
    serialized = response.text
    for forbidden in (
        "orderId",
        "buyer",
        "email",
        "phone",
        "inn",
        "kpp",
        "idempotency",
        "token",
        "outbox",
        "integrationStatus",
    ):
        assert forbidden not in serialized


def test_session_cannot_read_another_sessions_order(
    app: FastAPI,
    order_payload_factory,
) -> None:
    with TestClient(app) as owner, TestClient(app) as stranger:
        owned = _create_order(
            owner,
            order_payload_factory(),
            key="customer-owner-key-0001",
        )
        other_payload = order_payload_factory()
        other_payload["comment"] = "Другой заказ"
        stranger_order = _create_order(
            stranger,
            other_payload,
            key="customer-stranger-key-0001",
        )
        assert stranger_order.status_code == 201

        forbidden = stranger.get(
            f'/api/customer/orders/{owned.json()["orderNumber"]}'
        )
        missing = stranger.get("/api/customer/orders/AD-2026-999999")

    assert _error_signature(forbidden) == _error_signature(missing)
    assert _error_signature(forbidden)[:2] == (404, "ORDER_NOT_AVAILABLE")


def test_missing_unknown_expired_and_revoked_sessions_are_indistinguishable(
    app: FastAPI,
    order_payload_factory,
) -> None:
    with TestClient(app) as owner:
        created = _create_order(
            owner,
            order_payload_factory(),
            key="customer-lifecycle-key-0001",
        )
        order_number = created.json()["orderNumber"]
        raw_token = owner.cookies.get(COOKIE_NAME)
        assert raw_token is not None

        owner.cookies.clear()
        no_cookie = owner.get(f"/api/customer/orders/{order_number}")

        owner.cookies.set(COOKIE_NAME, "A" * 43, path="/api")
        unknown = owner.get(f"/api/customer/orders/{order_number}")

        with app.state.context.database.transaction() as session:
            stored = session.scalar(select(CustomerSessionModel))
            assert stored is not None
            stored.created_at -= 100
            stored.last_used_at = stored.created_at
            stored.expires_at = stored.created_at + 1
        owner.cookies.set(COOKIE_NAME, raw_token, path="/api")
        expired = owner.get(f"/api/customer/orders/{order_number}")

        with app.state.context.database.transaction() as session:
            stored = session.scalar(select(CustomerSessionModel))
            assert stored is not None
            stored.expires_at = int(time.time()) + 10_000
            stored.revoked_at = int(time.time())
        revoked = owner.get(f"/api/customer/orders/{order_number}")

    signatures = {
        _error_signature(response)
        for response in (no_cookie, unknown, expired, revoked)
    }
    assert signatures == {
        (404, "ORDER_NOT_AVAILABLE", "Заказ недоступен.", ())
    }


def test_owner_can_download_immutable_pdf_but_stranger_and_missing_cookie_cannot(
    app: FastAPI,
    order_payload_factory,
) -> None:
    with TestClient(app) as owner, TestClient(app) as stranger:
        created = _create_order(
            owner,
            order_payload_factory(),
            key="customer-pdf-key-0001",
        )
        order_id = created.json()["orderId"]
        order_number = created.json()["orderNumber"]
        before = app.state.context.invoices.get_for_order(order_id)
        assert before is not None and before.pdf_sha256 is not None

        inline = owner.get(
            f"/api/customer/orders/{order_number}/invoice.pdf?disposition=inline"
        )
        attachment = owner.get(
            f"/api/customer/orders/{order_number}/invoice.pdf"
        )
        forbidden = stranger.get(
            f"/api/customer/orders/{order_number}/invoice.pdf"
        )
        stranger.cookies.set(COOKIE_NAME, "B" * 43, path="/api")
        unknown = stranger.get(
            f"/api/customer/orders/{order_number}/invoice.pdf"
        )

    after = app.state.context.invoices.get_for_order(order_id)
    assert inline.status_code == 200
    assert inline.headers["content-type"] == "application/pdf"
    assert inline.headers["content-disposition"].startswith("inline;")
    assert attachment.headers["content-disposition"].startswith("attachment;")
    assert inline.content.startswith(b"%PDF")
    assert inline.content == attachment.content
    assert before.pdf_sha256 == after.pdf_sha256  # type: ignore[union-attr]
    assert _error_signature(forbidden) == _error_signature(unknown)
    assert forbidden.status_code == 404


def test_one_session_can_access_multiple_orders(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    first = _create_order(
        client,
        order_payload_factory(),
        key="customer-multi-key-0001",
    )
    first_token = client.cookies.get(COOKIE_NAME)
    second_payload = order_payload_factory()
    second_payload["comment"] = "Следующий заказ в той же сессии"
    second = _create_order(
        client,
        second_payload,
        key="customer-multi-key-0002",
    )

    assert first.status_code == second.status_code == 201
    assert client.cookies.get(COOKIE_NAME) == first_token
    assert app.state.context.customer_sessions.count() == 1
    assert app.state.context.customer_sessions.grant_count() == 2
    assert client.get(
        f'/api/customer/orders/{first.json()["orderNumber"]}'
    ).status_code == 200
    assert client.get(
        f'/api/customer/orders/{second.json()["orderNumber"]}'
    ).status_code == 200


def test_idempotency_replay_does_not_duplicate_or_grant_access_to_stranger(
    app: FastAPI,
    order_payload_factory,
) -> None:
    payload = order_payload_factory()
    with TestClient(app) as owner, TestClient(app) as stranger:
        first = _create_order(
            owner,
            payload,
            key="customer-idempotency-key-0001",
        )
        replay = _create_order(
            owner,
            payload,
            key="customer-idempotency-key-0001",
        )
        outsider_replay = _create_order(
            stranger,
            payload,
            key="customer-idempotency-key-0001",
        )
        denied = stranger.get(
            f'/api/customer/orders/{first.json()["orderNumber"]}'
        )

    assert first.status_code == 201
    assert replay.status_code == outsider_replay.status_code == 200
    assert "set-cookie" not in replay.headers
    assert "set-cookie" not in outsider_replay.headers
    assert denied.status_code == 404
    assert app.state.context.customer_sessions.count() == 1
    assert app.state.context.customer_sessions.grant_count() == 1
    assert app.state.context.invoices.count() == 1
    with app.state.context.database.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderModel)) == 1
        assert session.scalar(
            select(func.count()).select_from(InvoiceModel)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(IdempotencyRecordModel)
        ) == 1


def test_logout_revokes_session_and_clears_cookie(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-logout-key-0001",
    )
    order_number = created.json()["orderNumber"]

    logout = client.post("/api/customer/session/logout")
    denied = client.get(f"/api/customer/orders/{order_number}")

    assert logout.status_code == 204
    assert f"{COOKIE_NAME}=\"\"" in logout.headers["set-cookie"]
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert denied.status_code == 404
    with app.state.context.database.session() as session:
        stored = session.scalar(select(CustomerSessionModel))
        assert stored is not None and stored.revoked_at is not None


def test_order_number_format_is_public_but_not_an_authorization_secret(
    client: TestClient,
    order_payload_factory,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-number-key-0001",
    )
    order_number = created.json()["orderNumber"]
    assert re.fullmatch(r"AD-\d{4}-\d{6}", order_number)

    client.cookies.clear()
    assert client.get(f"/api/customer/orders/{order_number}").status_code == 404
