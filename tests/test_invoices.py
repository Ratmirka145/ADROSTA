from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import func, select

from app.models import InvoiceModel, OrderModel


def _create_order(client: TestClient, payload: dict, *, key: str):
    return client.post(
        "/api/orders",
        json=payload,
        headers={"Idempotency-Key": key},
    )


def test_invoice_is_generated_from_immutable_order_snapshot(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    payload = order_payload_factory(business=True)
    response = _create_order(client, payload, key="invoice-snapshot-key-0001")
    assert response.status_code == 201

    invoice = app.state.context.invoices.get_for_order(response.json()["orderId"])
    assert invoice is not None
    assert invoice.status == "generated"
    assert re.fullmatch(r"INV-\d{4}-\d{6}", invoice.invoice_number)
    assert invoice.products_amount_kopecks == 600_000
    assert invoice.delivery_amount_kopecks == 0
    assert invoice.grand_total_kopecks == 600_000
    assert invoice.seller_legal_name == "ООО АДРОСТА ТЕСТ"
    assert invoice.seller_inn == "7707083893"
    assert invoice.buyer_name == "ООО Тест"
    assert invoice.buyer_inn == "7707083893"
    assert invoice.buyer_kpp == "773601001"
    assert invoice.payment_purpose.startswith("Оплата по счёту INV-")
    assert [item.line_type for item in invoice.items] == ["product", "product"]
    assert invoice.items[0].quantity == 20
    assert invoice.items[0].unit == "шт."
    assert invoice.items[0].unit_price_kopecks == 10_000
    assert invoice.items[0].line_amount_kopecks == 200_000


def test_invoice_pdf_has_cyrillic_content_and_matching_sha256(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    response = _create_order(
        client, order_payload_factory(business=True), key="invoice-pdf-key-0001"
    )
    invoice = app.state.context.invoices.get_for_order(response.json()["orderId"])
    assert invoice is not None and invoice.pdf_bytes is not None
    assert invoice.pdf_bytes.startswith(b"%PDF")
    assert len(invoice.pdf_bytes) > 5_000
    assert invoice.pdf_sha256 == sha256(invoice.pdf_bytes).hexdigest()
    text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(invoice.pdf_bytes)).pages
    )
    assert "Счёт на оплату" in text
    assert "Поставщик" in text
    assert "Покупатель" in text
    assert "Без НДС (тест)" in text
    assert "₽" in text


def test_self_pickup_has_no_zero_delivery_line(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    response = _create_order(
        client, order_payload_factory(), key="invoice-self-pickup-key-0001"
    )
    invoice = app.state.context.invoices.get_for_order(response.json()["orderId"])
    assert invoice is not None
    assert all(item.line_type != "delivery" for item in invoice.items)
    assert all(item.name != "Доставка СДЭК" for item in invoice.items)


def test_catalog_and_seller_changes_do_not_mutate_existing_invoice(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    response = _create_order(
        client, order_payload_factory(business=True), key="invoice-immutable-key-0001"
    )
    order_id = response.json()["orderId"]
    original = app.state.context.invoices.get_for_order(order_id)
    assert original is not None

    products = app.state.context.products
    from app.repositories import ProductPriceTier

    products.replace_price_tiers("ADR-001", (ProductPriceTier(1, None, 99_999),))
    app.state.context.invoice_service.settings = replace(
        app.state.context.settings,
        seller_legal_name="ООО НОВЫЕ РЕКВИЗИТЫ",
        invoice_tax_text="Иной налоговый текст",
    )
    ensured = app.state.context.invoice_service.ensure_invoice(order_id)

    assert ensured.invoice_number == original.invoice_number
    assert ensured.seller_legal_name == "ООО АДРОСТА ТЕСТ"
    assert ensured.tax_text == "Без НДС (тест)"
    assert ensured.items[0].unit_price_kopecks == 10_000
    assert ensured.pdf_sha256 == original.pdf_sha256


def test_idempotency_replay_keeps_one_order_and_one_invoice(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    payload = order_payload_factory()
    first = _create_order(client, payload, key="invoice-replay-key-0001")
    replay = _create_order(client, payload, key="invoice-replay-key-0001")
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json()["orderId"] == replay.json()["orderId"]
    assert app.state.context.invoices.count() == 1
    first_invoice = app.state.context.invoices.get_for_order(first.json()["orderId"])
    second_invoice = app.state.context.invoice_service.ensure_invoice(
        first.json()["orderId"]
    )
    assert first_invoice is not None
    assert first_invoice.id == second_invoice.id
    assert first_invoice.invoice_number == second_invoice.invoice_number


def test_invoice_numbers_are_unique_and_sequential(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    numbers = []
    for number in (1, 2):
        payload = order_payload_factory()
        payload["comment"] = f"invoice sequence {number}"
        response = _create_order(
            client, payload, key=f"invoice-sequence-key-{number:04d}"
        )
        invoice = app.state.context.invoices.get_for_order(response.json()["orderId"])
        assert invoice is not None
        numbers.append(invoice.invoice_number)
    assert numbers[0][:-6] == numbers[1][:-6]
    assert int(numbers[1][-6:]) == int(numbers[0][-6:]) + 1
    assert len(set(numbers)) == 2


class FailingRenderer:
    def render(self, invoice):
        del invoice
        raise RuntimeError("renderer failed with internal details")


def test_pdf_failure_is_retry_safe_and_does_not_duplicate_order(
    client: TestClient,
    app: FastAPI,
    order_payload_factory,
) -> None:
    context = app.state.context
    working_renderer = context.invoice_service.renderer
    context.invoice_service.renderer = FailingRenderer()
    payload = order_payload_factory()

    first = _create_order(client, payload, key="invoice-failure-key-0001")
    replay = _create_order(client, payload, key="invoice-failure-key-0001")
    assert first.status_code == 201
    assert replay.status_code == 200
    assert context.invoices.count() == 1
    failed = context.invoices.get_for_order(first.json()["orderId"])
    assert failed is not None and failed.status == "failed"

    context.invoice_service.renderer = working_renderer
    summary = context.outbox_processor().process_once()
    generated = context.invoices.get_for_order(first.json()["orderId"])
    assert summary.delivered == 1
    assert generated is not None and generated.status == "generated"
    with context.database.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderModel)) == 1
        assert session.scalar(select(func.count()).select_from(InvoiceModel)) == 1


def test_missing_seller_config_rejects_before_order_creation(
    app_factory,
    order_payload_factory,
) -> None:
    application = app_factory(SELLER_LEGAL_NAME="")
    with TestClient(application) as client:
        response = _create_order(
            client,
            order_payload_factory(),
            key="invoice-not-configured-key-0001",
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INVOICE_NOT_CONFIGURED"
    assert application.state.context.invoices.count() == 0
    with application.state.context.database.session() as session:
        assert session.scalar(select(func.count()).select_from(OrderModel)) == 0


def test_openapi_exposes_only_session_protected_invoice_download(app: FastAPI) -> None:
    invoice_paths = [path for path in app.openapi()["paths"] if "invoice" in path]
    assert invoice_paths == [
        "/api/customer/orders/{order_number}/invoice.pdf"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("invoiceNumber", "INV-2026-999999"),
        ("invoiceTotalKopecks", 1),
        ("seller", {"bank": "attacker-controlled"}),
        ("taxText", "attacker-controlled"),
        ("invoicePdf", "JVBERi0="),
    ],
)
def test_order_rejects_frontend_invoice_data(
    client: TestClient,
    order_payload_factory,
    field: str,
    value,
) -> None:
    payload = order_payload_factory()
    payload[field] = value
    response = _create_order(
        client,
        payload,
        key=f"invoice-untrusted-{field.lower()}-key-0001",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
