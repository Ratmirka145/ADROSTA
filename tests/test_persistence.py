from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.domain import calculate_order
from app.models import (
    IdempotencyRecordModel,
    OrderItemModel,
    OrderModel,
    OutboxModel,
)
from app.repositories import OrderDraft, OrderItemInput


def test_order_transaction_rolls_back_all_rows_on_midway_failure(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory()
    context = application.state.context
    assert context.orders is not None
    calculation = calculate_order(
        (OrderItemInput(sku="ADR-001", boxes=2),),
        context.products.fetch_catalog(("ADR-001",)),
    )
    draft = OrderDraft(
        buyer_type="individual",
        buyer_contact_name="Иван Петров",
        buyer_phone="+79991234567",
        buyer_email="buyer@example.com",
        delivery_method="self_pickup",
        items=(OrderItemInput(sku="ADR-001", boxes=2),),
    )

    def fail_after_parent_flush(session: Session, flush_context: object) -> None:
        del session, flush_context
        raise RuntimeError("injected failure after order insert")

    event.listen(Session, "after_flush_postexec", fail_after_parent_flush, once=True)
    with pytest.raises(RuntimeError, match="injected failure"):
        context.orders.create_order(
            draft,
            calculation=calculation,
            request_hash="rollback-request",
            duplicate_fingerprint="rollback-fingerprint",
            idempotency_key="rollback-idempotency-key-0001",
            enqueue_outbox=True,
        )

    with context.database.session() as session:
        for model in (
            OrderModel,
            OrderItemModel,
            IdempotencyRecordModel,
            OutboxModel,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
