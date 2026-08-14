from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations import DestinationError
from app.services import OutboxProcessor


class UnavailableDestination:
    def send(self, *, order_id: str, payload: Mapping[str, Any]):
        raise DestinationError("DESTINATION_UNAVAILABLE", retryable=True)


def test_external_service_outage_keeps_order_in_durable_outbox(
    app_factory: Callable[..., FastAPI],
    order_payload_factory,
) -> None:
    application = app_factory(
        WEBHOOK_ENABLED=True,
        ORDER_WEBHOOK_URL="https://sink.example.test/orders",
        OUTBOX_ENABLED=True,
        WEBHOOK_MAX_ATTEMPTS=3,
    )
    context = application.state.context
    assert context.orders is not None

    with TestClient(application) as client:
        accepted = client.post(
            "/api/orders",
            json=order_payload_factory(),
            headers={"Idempotency-Key": "outage-order-key-0001"},
        )

        processor = OutboxProcessor(
            settings=context.settings,
            orders=context.orders,
            outbox=context.outbox,
            destination=UnavailableDestination(),
        )
        summary = processor.process_once()
        replay = client.post(
            "/api/orders",
            json=order_payload_factory(),
            headers={"Idempotency-Key": "outage-order-key-0001"},
        )

    assert accepted.status_code == 201
    assert accepted.json()["integrationStatus"] == "pending"
    assert summary.claimed == 1
    assert summary.retry_scheduled == 1
    assert summary.delivered == 0
    assert context.outbox.count(status="pending") == 1
    assert context.orders.get_order_response(accepted.json()["orderId"]) is not None
    assert replay.status_code == 200
    assert replay.json()["integrationStatus"] == "pending"
