from __future__ import annotations

import hashlib
from pathlib import Path
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
    OutboxModel,
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
            "status": "generated",
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


def test_last_used_at_is_touched_at_a_bounded_interval(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-last-used-key-0001",
    )
    token = client.cookies.get(COOKIE_NAME)
    assert token is not None
    with app.state.context.database.session() as session:
        stored = session.scalar(select(CustomerSessionModel))
        assert stored is not None
        initial = stored.last_used_at

    app.state.context.customer_sessions.lookup_order(
        token,
        created.json()["orderNumber"],
        now=initial + 3599,
    )
    with app.state.context.database.session() as session:
        stored = session.scalar(select(CustomerSessionModel))
        assert stored is not None and stored.last_used_at == initial

    app.state.context.customer_sessions.lookup_order(
        token,
        created.json()["orderNumber"],
        now=initial + 3600,
    )
    with app.state.context.database.session() as session:
        stored = session.scalar(select(CustomerSessionModel))
        assert stored is not None and stored.last_used_at == initial + 3600


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
        missing = stranger.get(
            "/api/customer/orders/AD-2026-999999/invoice.pdf"
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
    assert _error_signature(forbidden) == _error_signature(missing)
    assert forbidden.status_code == 404


def test_pending_and_failed_invoice_are_visible_but_not_downloadable(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-pending-invoice-key-0001",
    )
    order_id = created.json()["orderId"]
    order_number = created.json()["orderNumber"]

    with app.state.context.database.transaction() as session:
        invoice = session.scalar(
            select(InvoiceModel).where(InvoiceModel.order_id == order_id)
        )
        assert invoice is not None
        invoice.status = "pending"
        invoice.pdf_content = None
        invoice.pdf_sha256 = None
        invoice.generated_at = None

    class RendererMustNotRun:
        def render(self, invoice):
            del invoice
            raise AssertionError("customer GET must not render an invoice")

    original_renderer = app.state.context.invoice_service.renderer
    app.state.context.invoice_service.renderer = RendererMustNotRun()
    try:
        pending_order = client.get(f"/api/customer/orders/{order_number}")
        pending_pdf = client.get(
            f"/api/customer/orders/{order_number}/invoice.pdf"
        )
    finally:
        app.state.context.invoice_service.renderer = original_renderer

    assert pending_order.status_code == 200
    assert pending_order.json()["invoice"]["status"] == "pending"
    assert pending_order.json()["invoice"]["pdfAvailable"] is False
    assert pending_pdf.status_code == 409
    assert pending_pdf.json()["error"]["code"] == "INVOICE_NOT_READY"

    with app.state.context.database.transaction() as session:
        invoice = session.scalar(
            select(InvoiceModel).where(InvoiceModel.order_id == order_id)
        )
        assert invoice is not None
        invoice.status = "failed"

    failed_order = client.get(f"/api/customer/orders/{order_number}")
    failed_pdf = client.get(f"/api/customer/orders/{order_number}/invoice.pdf")
    assert failed_order.status_code == 200
    assert failed_order.json()["invoice"]["status"] == "failed"
    assert failed_order.json()["invoice"]["pdfAvailable"] is False
    assert failed_pdf.status_code == 409
    assert failed_pdf.json()["error"]["code"] == "INVOICE_NOT_READY"


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
        raw_token = owner.cookies.get(COOKIE_NAME)
        assert raw_token is not None
        idempotency = session.scalar(select(IdempotencyRecordModel))
        outbox_rows = session.scalars(select(OutboxModel)).all()
        assert idempotency is not None
        persisted_values = [
            value
            for row in (idempotency, *outbox_rows)
            for value in vars(row).values()
            if isinstance(value, str)
        ]
        assert all(raw_token not in value for value in persisted_values)


def test_logout_revokes_session_and_clears_cookie(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
    allowed_origin: str,
) -> None:
    created = _create_order(
        client,
        order_payload_factory(),
        key="customer-logout-key-0001",
    )
    order_number = created.json()["orderNumber"]

    missing_origin = client.post("/api/customer/session/logout")
    wrong_origin = client.post(
        "/api/customer/session/logout",
        headers={"Origin": "https://attacker.example"},
    )
    still_available = client.get(f"/api/customer/orders/{order_number}")
    logout = client.post(
        "/api/customer/session/logout",
        headers={"Origin": allowed_origin},
    )
    denied = client.get(f"/api/customer/orders/{order_number}")

    assert missing_origin.status_code == wrong_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "FORBIDDEN"
    assert wrong_origin.json()["error"]["code"] == "FORBIDDEN"
    assert still_available.status_code == 200
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


def test_tilda_example_uses_bounded_credentialed_polling_and_safe_dom() -> None:
    page_source = (
        Path(__file__).parents[1] / "examples" / "tilda-order-page.html"
    ).read_text(encoding="utf-8")
    client_source = (
        Path(__file__).parents[1] / "examples" / "tilda-api-client.js"
    ).read_text(encoding="utf-8")

    assert 'credentials: "include"' in page_source
    assert "INVOICE_POLL_INTERVAL_MS = 4000" in page_source
    assert "INVOICE_POLL_MAX_ATTEMPTS = 15" in page_source
    assert "window.setTimeout" in page_source
    assert "innerHTML" not in page_source
    assert page_source.count("window.localStorage.setItem") == 1
    assert 'window.localStorage.setItem("last_order_number"' in page_source
    assert 'credentials: "include"' in client_source
    assert 'typeof body.orderPageUrl !== "string"' in client_source
