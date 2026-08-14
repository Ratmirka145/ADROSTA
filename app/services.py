from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

from app.config import ConfigError, Settings
from app.errors import (
    CatalogUnavailableError,
    DuplicateOrderError,
    IdempotencyConflictError as ApiIdempotencyConflictError,
    IdempotencyKeyRequiredError,
    RateLimitError,
    ServiceUnavailableError,
    UnknownSkuError,
    ValidationAppError,
)
from app.integrations import DestinationError, OrderDestination
from app.repositories import (
    IdempotencyConflictError as RepositoryIdempotencyConflictError,
    InvalidOrderError,
    OrderDraft,
    OrderItemInput,
    OrderRepository,
    OutboxRepository,
    ProductCatalogError,
    ProductRepository,
    RateLimitResult,
    RateLimitRepository,
    RepositoryError,
    UnknownProductError,
)
from app.schemas import (
    CalculatedItemResponse,
    OrderCreate,
    OrderResponse,
    OrderTotalsResponse,
)
from app.security import canonical_hmac, is_valid_idempotency_key


logger = logging.getLogger("adrosta.orders")
_GRAMS_PER_KILOGRAM = Decimal(1_000)
_MM3_PER_M3 = Decimal(1_000_000_000)


@dataclass(frozen=True, slots=True)
class CreateOrderOutcome:
    response: OrderResponse
    created: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProcessingSummary:
    claimed: int = 0
    delivered: int = 0
    retry_scheduled: int = 0
    failed: int = 0


class OrderService:
    def __init__(
        self,
        *,
        settings: Settings,
        products: ProductRepository,
        orders: OrderRepository | None,
        outbox: OutboxRepository,
        rate_limits: RateLimitRepository | None,
    ) -> None:
        self.settings = settings
        self.products = products
        self.orders = orders
        self.outbox = outbox
        self.rate_limits = rate_limits

    def create_order(
        self,
        order: OrderCreate,
        *,
        idempotency_key: str | None,
        client_ip: str,
        rate_limit_result: RateLimitResult | None = None,
        rate_limit_error: bool = False,
        now: int | None = None,
    ) -> CreateOrderOutcome:
        if (
            not is_valid_idempotency_key(idempotency_key)
            or idempotency_key is None
            or len(idempotency_key) > self.settings.idempotency_key_max_length
        ):
            raise IdempotencyKeyRequiredError()

        if self.orders is None or self.rate_limits is None:
            raise ServiceUnavailableError()

        self._validate_runtime_limits(order)

        try:
            secret = self.settings.app_hash_secret
            if not secret:
                raise ConfigError("APP_HASH_SECRET is not configured")
            normalized_payload = order.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            )
            canonical_payload = dict(normalized_payload)
            canonical_payload["items"] = sorted(
                normalized_payload["items"],
                key=lambda item: item["sku"],
            )
            fingerprint = canonical_hmac(secret, canonical_payload)
            replay = self.orders.lookup_idempotency(
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
                now=now,
            )
        except RepositoryIdempotencyConflictError:
            raise ApiIdempotencyConflictError() from None
        except (ConfigError, sqlite3.Error, RepositoryError):
            raise ServiceUnavailableError() from None

        if replay is not None:
            integration_status = self._integration_status(replay.order_id)
            return CreateOrderOutcome(
                response=self._render_response(
                    replay,
                    integration_status=integration_status,
                    replayed=True,
                ),
                created=False,
                replayed=True,
            )

        if rate_limit_error:
            raise ServiceUnavailableError()
        if rate_limit_result is None:
            try:
                rate_limit_result = self.rate_limits.consume(
                    client_ip,
                    limit=self.settings.order_rate_limit_count,
                    window_seconds=self.settings.order_rate_limit_window_seconds,
                    now=now,
                )
            except sqlite3.Error:
                raise ServiceUnavailableError() from None
        if not rate_limit_result.allowed:
            raise RateLimitError(
                retry_after=max(1, rate_limit_result.retry_after_seconds)
            )

        try:
            if self.products.count() == 0:
                raise CatalogUnavailableError()
        except CatalogUnavailableError:
            raise
        except sqlite3.Error:
            raise CatalogUnavailableError() from None

        try:
            result = self.orders.create_order(
                self._to_draft(order),
                request_hash=fingerprint,
                duplicate_fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                enqueue_outbox=(
                    self.settings.webhook_enabled and self.settings.outbox_enabled
                ),
                now=now,
            )
        except UnknownProductError:
            raise UnknownSkuError() from None
        except ProductCatalogError:
            raise CatalogUnavailableError() from None
        except RepositoryIdempotencyConflictError:
            raise ApiIdempotencyConflictError() from None
        except InvalidOrderError:
            raise ValidationAppError() from None
        except ConfigError:
            raise ServiceUnavailableError() from None
        except sqlite3.Error:
            raise ServiceUnavailableError() from None
        except RepositoryError:
            raise ServiceUnavailableError() from None

        if result.duplicate:
            raise DuplicateOrderError()

        integration_status = self._integration_status(result.response.order_id)
        return CreateOrderOutcome(
            response=self._render_response(
                result.response,
                integration_status=integration_status,
                replayed=result.replayed,
            ),
            created=result.created,
            replayed=result.replayed,
        )

    def readiness(self) -> ReadinessResult:
        checks = {
            "configuration": "ok" if self.settings.app_hash_secret else "missing_secret",
            "database": "unavailable",
            "catalog": "unavailable",
            "destination": "configured" if self.settings.webhook_enabled else "store_only",
        }
        try:
            with self.products.database.connection() as connection:
                connection.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
            checks["catalog"] = "ok" if self.products.count() > 0 else "empty"
        except sqlite3.Error:
            pass

        destination_ready = self.settings.webhook_enabled or self.settings.allow_store_only
        ready = (
            checks["database"] == "ok"
            and checks["catalog"] == "ok"
            and checks["configuration"] == "ok"
            and destination_ready
        )
        if not destination_ready:
            checks["destination"] = "required"
        return ReadinessResult(ready=ready, checks=checks)

    def _validate_runtime_limits(self, order: OrderCreate) -> None:
        if len(order.items) > self.settings.max_order_items:
            raise ValidationAppError()
        total_boxes = 0
        for item in order.items:
            if item.boxes > self.settings.max_boxes_per_item:
                raise ValidationAppError()
            total_boxes += item.boxes
        if total_boxes > self.settings.max_total_boxes:
            raise ValidationAppError()

    @staticmethod
    def _to_draft(order: OrderCreate) -> OrderDraft:
        company = order.company
        recipient = order.delivery.recipient
        return OrderDraft(
            buyer_type=order.buyer.type.value,
            buyer_contact_name=order.buyer.contact_name,
            buyer_phone=order.buyer.phone,
            buyer_email=str(order.buyer.email),
            company_name=company.name if company else None,
            company_inn=company.inn if company else None,
            company_kpp=company.kpp if company else None,
            company_legal_address=company.legal_address if company else None,
            delivery_method=order.delivery.method.value,
            delivery_type=(
                order.delivery.type.value if order.delivery.type is not None else None
            ),
            delivery_region=order.delivery.region,
            delivery_city=order.delivery.city,
            delivery_office_code=order.delivery.office_code,
            delivery_postcode=order.delivery.postcode,
            delivery_street=order.delivery.street,
            delivery_house=order.delivery.house,
            delivery_apartment=order.delivery.apartment,
            recipient_contact_name=(recipient.contact_name if recipient else None),
            recipient_phone=(recipient.phone if recipient else None),
            recipient_email=(
                str(recipient.email) if recipient and recipient.email else None
            ),
            comment=order.comment,
            items=tuple(
                OrderItemInput(sku=item.sku, boxes=item.boxes) for item in order.items
            ),
        )

    def _integration_status(
        self,
        order_id: str,
    ) -> Literal["pending", "stored", "delivered", "failed"]:
        try:
            message = self.outbox.get_for_order(order_id)
        except sqlite3.Error:
            return "pending" if self.settings.webhook_enabled else "stored"
        if message is None:
            return "stored"
        return {
            "pending": "pending",
            "processing": "pending",
            "succeeded": "delivered",
            "failed": "failed",
        }.get(message.status, "pending")  # type: ignore[return-value]

    @staticmethod
    def _render_response(
        stored,
        *,
        integration_status: Literal["pending", "stored", "delivered", "failed"],
        replayed: bool,
    ) -> OrderResponse:
        items = [
            CalculatedItemResponse(
                sku=item.sku,
                boxes=item.boxes,
                box_weight_grams=item.unit_weight_grams,
                box_volume_mm3=item.unit_volume_mm3,
                length_mm=item.box_length_mm,
                width_mm=item.box_width_mm,
                height_mm=item.box_height_mm,
                weight_grams=item.total_weight_grams,
                volume_mm3=item.total_volume_mm3,
                weight_kg=(Decimal(item.total_weight_grams) / _GRAMS_PER_KILOGRAM),
                volume_m3=(Decimal(item.total_volume_mm3) / _MM3_PER_M3),
            )
            for item in stored.items
        ]
        totals = OrderTotalsResponse(
            boxes=stored.total_boxes,
            weight_grams=stored.total_weight_grams,
            volume_mm3=stored.total_volume_mm3,
            weight_kg=Decimal(stored.total_weight_grams) / _GRAMS_PER_KILOGRAM,
            volume_m3=Decimal(stored.total_volume_mm3) / _MM3_PER_M3,
        )
        return OrderResponse(
            order_id=UUID(stored.order_id),
            status="accepted",
            integration_status=integration_status,
            replayed=replayed,
            items=items,
            totals=totals,
        )


class OutboxProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        orders: OrderRepository,
        outbox: OutboxRepository,
        destination: OrderDestination,
    ) -> None:
        self.settings = settings
        self.orders = orders
        self.outbox = outbox
        self.destination = destination

    def process_once(self, *, now: int | None = None) -> ProcessingSummary:
        claimed = delivered = retry_scheduled = failed = 0
        for _ in range(self.settings.outbox_batch_size):
            messages = self.outbox.claim(
                limit=1,
                lease_seconds=self.settings.outbox_lock_seconds,
                max_attempts=self.settings.webhook_max_attempts,
                now=now,
            )
            if not messages:
                break
            message = messages[0]
            claimed += 1
            token = message.lock_token
            if not token:
                continue
            order = self.orders.get_order_for_webhook(message.order_id)
            if order is None:
                self.outbox.mark_failed(
                    message.id,
                    token,
                    "ORDER_NOT_FOUND",
                    now=now,
                )
                failed += 1
                continue
            try:
                self.destination.send(
                    order_id=message.order_id,
                    payload=order.as_dict(),
                )
            except DestinationError as exc:
                if exc.retryable and message.attempt_count < self.settings.webhook_max_attempts:
                    delay = self._retry_delay(message.attempt_count)
                    self.outbox.mark_retry(
                        message.id,
                        token,
                        exc.code,
                        delay_seconds=delay,
                        now=now,
                    )
                    retry_scheduled += 1
                else:
                    self.outbox.mark_failed(message.id, token, exc.code, now=now)
                    failed += 1
            except Exception:
                if message.attempt_count < self.settings.webhook_max_attempts:
                    self.outbox.mark_retry(
                        message.id,
                        token,
                        "DESTINATION_UNEXPECTED_ERROR",
                        delay_seconds=self._retry_delay(message.attempt_count),
                        now=now,
                    )
                    retry_scheduled += 1
                else:
                    self.outbox.mark_failed(
                        message.id,
                        token,
                        "DESTINATION_UNEXPECTED_ERROR",
                        now=now,
                    )
                    failed += 1
            else:
                self.outbox.mark_success(message.id, token, now=now)
                delivered += 1

        return ProcessingSummary(
            claimed=claimed,
            delivered=delivered,
            retry_scheduled=retry_scheduled,
            failed=failed,
        )

    def _retry_delay(self, attempt_count: int) -> int:
        delay = self.settings.webhook_backoff_seconds * (2 ** max(0, attempt_count - 1))
        return max(1, min(3_600, math.ceil(delay)))
