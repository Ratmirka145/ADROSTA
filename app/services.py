from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TYPE_CHECKING
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from app.config import ConfigError, Settings
from app.domain import (
    AmbiguousPriceTierError,
    CatalogIntegrityError,
    DuplicateSkuError as DomainDuplicateSkuError,
    InvalidOrderItemError,
    OrderCalculation,
    PriceTierNotFoundError as DomainPriceTierNotFoundError,
    UnknownSkuError as DomainUnknownSkuError,
    calculate_order,
)
from app.errors import (
    CatalogUnavailableError,
    CdekNotConfiguredError,
    DuplicateSkuError,
    DuplicateOrderError,
    IdempotencyConflictError as ApiIdempotencyConflictError,
    IdempotencyKeyRequiredError,
    InvoiceGenerationFailedError,
    InvoiceNotConfiguredError,
    InvoiceNotReadyError,
    RateLimitError,
    PriceTierNotFoundError,
    ServiceUnavailableError,
    UnknownSkuError,
    ValidationAppError,
)
from app.integrations import DestinationError, OrderDestination
from app.repositories import (
    CustomerInvoicePdfRecord,
    CustomerOrderRecord,
    CustomerSessionIssue,
    CustomerSessionRepository,
    IdempotencyConflictError as RepositoryIdempotencyConflictError,
    InvalidOrderError,
    InvoiceRepository,
    OrderDraft,
    OrderItemInput,
    OrderRepository,
    OutboxRepository,
    ProductRepository,
    RateLimitResult,
    RateLimitRepository,
    RepositoryError,
)
from app.invoice import InvoiceDraft, InvoiceItemDraft, InvoicePdfRenderer, InvoiceSnapshot
from app.schemas import (
    CalculatedItemResponse,
    CdekTariffOptionResponse,
    CartCalculateRequest,
    CustomerInvoiceResponse,
    CustomerOrderDeliveryResponse,
    CustomerOrderItemResponse,
    CustomerOrderResponse,
    CustomerOrderTotalsResponse,
    DeliveryMethod,
    OrderCommercialTotalsResponse,
    OrderCalculationResponse,
    OrderCreate,
    OrderDeliveryResponse,
    OrderResponse,
)
from app.security import canonical_hmac, is_valid_idempotency_key


logger = logging.getLogger("adrosta.orders")

if TYPE_CHECKING:
    from app.cdek import CdekService


@dataclass(frozen=True, slots=True)
class CreateOrderOutcome:
    response: OrderResponse
    created: bool
    replayed: bool
    customer_session_issue: CustomerSessionIssue | None = None


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


class CustomerAccessService:
    def __init__(self, customer_sessions: CustomerSessionRepository) -> None:
        self.customer_sessions = customer_sessions

    @staticmethod
    def _delivery_description(record: CustomerOrderRecord) -> str:
        if record.delivery_method == "self_pickup":
            return "Самовывоз"
        if record.delivery_type == "pickup":
            return (
                f"СДЭК, пункт выдачи {record.delivery_office_code}"
                if record.delivery_office_code
                else "СДЭК, пункт выдачи"
            )
        return "СДЭК, доставка до двери"

    def get_order(
        self,
        token: str | None,
        order_number: str,
        *,
        now: int | None = None,
    ) -> tuple[CustomerOrderResponse | None, bool]:
        try:
            lookup = self.customer_sessions.lookup_order(
                token,
                order_number,
                now=now,
            )
        except SQLAlchemyError:
            raise ServiceUnavailableError() from None
        if lookup.order is None:
            return None, lookup.session_valid
        record = lookup.order
        invoice = None
        if (
            record.invoice_number is not None
            and record.invoice_issued_at is not None
            and record.invoice_status is not None
        ):
            invoice = CustomerInvoiceResponse(
                number=record.invoice_number,
                issued_at=datetime.fromtimestamp(record.invoice_issued_at, UTC),
                status=record.invoice_status,
                pdf_available=record.invoice_pdf_available,
            )
        return (
            CustomerOrderResponse(
                order_number=record.order_number,
                status=record.status,  # type: ignore[arg-type]
                created_at=datetime.fromtimestamp(record.created_at, UTC),
                items=[
                    CustomerOrderItemResponse(
                        sku=item.sku,
                        name=item.name,
                        quantity=item.quantity,
                        unit=item.unit,
                        line_amount_kopecks=item.line_amount_kopecks,
                    )
                    for item in record.items
                ],
                totals=CustomerOrderTotalsResponse(
                    products_amount_kopecks=record.products_amount_kopecks,
                    delivery_amount_kopecks=record.delivery_amount_kopecks,
                    grand_total_kopecks=record.grand_total_kopecks,
                ),
                delivery=CustomerOrderDeliveryResponse(
                    method=record.delivery_method,  # type: ignore[arg-type]
                    type=record.delivery_type,  # type: ignore[arg-type]
                    description=self._delivery_description(record),
                    city=record.delivery_city,
                    office_code=record.delivery_office_code,
                    tariff_name=record.cdek_tariff_name,
                    period_min_days=record.cdek_period_min_days,
                    period_max_days=record.cdek_period_max_days,
                ),
                invoice=invoice,
            ),
            lookup.session_valid,
        )

    def get_invoice_pdf(
        self,
        token: str | None,
        order_number: str,
        *,
        now: int | None = None,
    ) -> tuple[CustomerInvoicePdfRecord | None, bool]:
        try:
            lookup = self.customer_sessions.lookup_invoice_pdf(
                token,
                order_number,
                now=now,
            )
        except SQLAlchemyError:
            raise ServiceUnavailableError() from None
        if lookup.invoice is None:
            if lookup.order_authorized:
                if lookup.invoice_status == "generated":
                    raise ServiceUnavailableError()
                raise InvoiceNotReadyError()
            return None, lookup.session_valid
        invoice = lookup.invoice
        actual_digest = hashlib.sha256(invoice.pdf_bytes).hexdigest()
        if (
            not invoice.pdf_bytes.startswith(b"%PDF")
            or not hmac.compare_digest(actual_digest, invoice.pdf_sha256)
        ):
            raise ServiceUnavailableError()
        return invoice, lookup.session_valid

    def logout(self, token: str | None) -> None:
        try:
            self.customer_sessions.revoke(token)
        except SQLAlchemyError:
            raise ServiceUnavailableError() from None


class InvoiceService:
    TEMPLATE_VERSION = "invoice-v1"

    def __init__(
        self,
        *,
        settings: Settings,
        invoices: InvoiceRepository,
        renderer: InvoicePdfRenderer,
    ) -> None:
        self.settings = settings
        self.invoices = invoices
        self.renderer = renderer

    def prepare_draft(
        self,
        order: OrderCreate,
        calculation: OrderCalculation,
        *,
        delivery_amount_kopecks: int,
    ) -> InvoiceDraft:
        if not self.settings.invoice_configured:
            raise InvoiceNotConfiguredError()
        company = order.company
        items = [
            InvoiceItemDraft(
                line_number=index,
                line_type="product",
                sku=item.sku,
                name=item.name,
                quantity=item.units,
                unit="шт.",
                unit_price_kopecks=item.price_per_unit_kopecks,
                line_amount_kopecks=item.line_amount_kopecks,
            )
            for index, item in enumerate(calculation.items, start=1)
        ]
        if delivery_amount_kopecks > 0:
            items.append(
                InvoiceItemDraft(
                    line_number=len(items) + 1,
                    line_type="delivery",
                    sku=None,
                    name="Доставка СДЭК",
                    quantity=1,
                    unit="усл.",
                    unit_price_kopecks=delivery_amount_kopecks,
                    line_amount_kopecks=delivery_amount_kopecks,
                )
            )
        products_amount = calculation.totals.products_amount_kopecks
        return InvoiceDraft(
            template_version=self.TEMPLATE_VERSION,
            seller_legal_name=self.settings.seller_legal_name or "",
            seller_inn=self.settings.seller_inn or "",
            seller_kpp=self.settings.seller_kpp or "",
            seller_legal_address=self.settings.seller_legal_address or "",
            seller_bank_name=self.settings.seller_bank_name or "",
            seller_bik=self.settings.seller_bik or "",
            seller_checking_account=self.settings.seller_checking_account or "",
            seller_correspondent_account=(
                self.settings.seller_correspondent_account or ""
            ),
            seller_phone=self.settings.seller_phone,
            seller_email=self.settings.seller_email,
            buyer_name=company.name if company else order.buyer.contact_name,
            buyer_inn=company.inn if company else None,
            buyer_kpp=company.kpp if company else None,
            buyer_legal_address=company.legal_address if company else None,
            products_amount_kopecks=products_amount,
            delivery_amount_kopecks=delivery_amount_kopecks,
            grand_total_kopecks=products_amount + delivery_amount_kopecks,
            tax_text=self.settings.invoice_tax_text or "",
            payment_purpose_template=(
                self.settings.invoice_payment_purpose_template or ""
            ),
            items=tuple(items),
        )

    def ensure_invoice(
        self, order_id: str, *, now: int | None = None
    ) -> InvoiceSnapshot:
        snapshot = self.invoices.get_for_order(order_id)
        if snapshot is None:
            raise InvoiceGenerationFailedError()
        if snapshot.status == "generated":
            return snapshot
        timestamp = int(time.time()) if now is None else int(now)
        try:
            pdf_bytes = self.renderer.render(snapshot)
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            return self.invoices.store_generated(
                snapshot.id,
                pdf_bytes=pdf_bytes,
                pdf_sha256=digest,
                generated_at=timestamp,
            )
        except Exception:
            try:
                self.invoices.mark_failed(snapshot.id, now=timestamp)
            except Exception as state_error:
                logger.error(
                    "invoice_failure_state_update_failed invoice_id=%s exception_type=%s",
                    snapshot.id,
                    type(state_error).__name__,
                )
            raise InvoiceGenerationFailedError() from None


class OrderService:
    def __init__(
        self,
        *,
        settings: Settings,
        products: ProductRepository,
        orders: OrderRepository | None,
        outbox: OutboxRepository,
        rate_limits: RateLimitRepository | None,
        cdek_service: CdekService | None = None,
        invoice_service: InvoiceService | None = None,
        customer_sessions: CustomerSessionRepository | None = None,
    ) -> None:
        self.settings = settings
        self.products = products
        self.orders = orders
        self.outbox = outbox
        self.rate_limits = rate_limits
        self.cdek_service = cdek_service
        self.invoice_service = invoice_service
        self.customer_sessions = customer_sessions

    def attach_cdek_service(self, cdek_service: CdekService) -> None:
        self.cdek_service = cdek_service

    def create_order(
        self,
        order: OrderCreate,
        *,
        idempotency_key: str | None,
        client_ip: str,
        rate_limit_result: RateLimitResult | None = None,
        rate_limit_error: bool = False,
        customer_session_token: str | None = None,
        request_id: str | None = None,
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
        except (ConfigError, SQLAlchemyError, RepositoryError):
            raise ServiceUnavailableError() from None

        if replay is not None:
            if self.customer_sessions is not None:
                try:
                    self.customer_sessions.has_order_access(
                        customer_session_token,
                        replay.order_id,
                        now=now,
                    )
                except SQLAlchemyError:
                    raise ServiceUnavailableError() from None
            self._ensure_invoice_safely(replay.order_id, now=now)
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
            except SQLAlchemyError:
                raise ServiceUnavailableError() from None
        if not rate_limit_result.allowed:
            raise RateLimitError(
                retry_after=max(1, rate_limit_result.retry_after_seconds)
            )

        calculation = self.calculate_items(order.items)
        selected_tariff: CdekTariffOptionResponse | None = None
        if order.delivery.method is DeliveryMethod.CDEK:
            if self.cdek_service is None:
                raise CdekNotConfiguredError()
            assert order.delivery.type is not None
            assert order.delivery.to_city_code is not None
            assert order.delivery.tariff_code is not None
            selected_tariff = self.cdek_service.verify_selected_tariff(
                delivery_type=order.delivery.type,
                to_city_code=order.delivery.to_city_code,
                tariff_code=order.delivery.tariff_code,
                office_code=order.delivery.office_code,
                calculation=calculation,
                request_id=request_id,
            )
        delivery_amount = (
            selected_tariff.delivery_amount_kopecks if selected_tariff else 0
        )
        if self.invoice_service is None:
            raise InvoiceNotConfiguredError()
        invoice_draft = self.invoice_service.prepare_draft(
            order,
            calculation,
            delivery_amount_kopecks=delivery_amount,
        )

        try:
            result = self.orders.create_order(
                self._to_draft(order, selected_tariff=selected_tariff),
                calculation=calculation,
                request_hash=fingerprint,
                duplicate_fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                enqueue_outbox=(
                    self.settings.webhook_enabled and self.settings.outbox_enabled
                ),
                invoice_draft=invoice_draft,
                enqueue_invoice_outbox=self.settings.outbox_enabled,
                customer_session_token=customer_session_token,
                customer_session_ttl_seconds=(
                    self.settings.customer_session_ttl_seconds
                ),
                now=now,
            )
        except RepositoryIdempotencyConflictError:
            raise ApiIdempotencyConflictError() from None
        except InvalidOrderError:
            raise ValidationAppError() from None
        except ConfigError:
            raise ServiceUnavailableError() from None
        except SQLAlchemyError:
            raise ServiceUnavailableError() from None
        except RepositoryError:
            raise ServiceUnavailableError() from None

        if result.duplicate:
            raise DuplicateOrderError()

        self._ensure_invoice_safely(result.response.order_id, now=now)

        integration_status = self._integration_status(result.response.order_id)
        return CreateOrderOutcome(
            response=self._render_response(
                result.response,
                integration_status=integration_status,
                replayed=result.replayed,
            ),
            created=result.created,
            replayed=result.replayed,
            customer_session_issue=result.customer_session_issue,
        )

    def _ensure_invoice_safely(self, order_id: str, *, now: int | None) -> None:
        if self.invoice_service is None:
            return
        try:
            self.invoice_service.ensure_invoice(order_id, now=now)
            self.outbox.complete_pending_event(
                order_id,
                event_type="invoice.generate",
                now=now,
            )
        except (InvoiceGenerationFailedError, SQLAlchemyError, RepositoryError):
            logger.warning("invoice_generation_deferred order_id=%s", order_id)

    def calculate_cart(
        self, request: CartCalculateRequest
    ) -> OrderCalculationResponse:
        self._validate_runtime_limits(request)
        return OrderCalculationResponse.from_domain(
            self.calculate_items(request.items)
        )

    def calculate_items(self, items) -> OrderCalculation:
        """Calculate trusted product and cargo data for internal consumers."""
        self._validate_items(items)
        try:
            if self.products.count() == 0:
                raise CatalogUnavailableError()
            products = self.products.fetch_catalog(item.sku for item in items)
            return calculate_order(items, products)
        except CatalogUnavailableError:
            raise
        except DomainUnknownSkuError:
            raise UnknownSkuError() from None
        except DomainDuplicateSkuError:
            raise DuplicateSkuError() from None
        except InvalidOrderItemError:
            raise ValidationAppError() from None
        except DomainPriceTierNotFoundError:
            raise PriceTierNotFoundError() from None
        except (AmbiguousPriceTierError, CatalogIntegrityError):
            raise CatalogUnavailableError() from None
        except SQLAlchemyError:
            raise CatalogUnavailableError() from None

    def readiness(self) -> ReadinessResult:
        checks = {
            "configuration": "ok" if self.settings.app_hash_secret else "missing_secret",
            "database": "unavailable",
            "catalog": "unavailable",
            "invoice": (
                "configured" if self.settings.invoice_configured else "not_configured"
            ),
            "destination": "configured" if self.settings.webhook_enabled else "store_only",
        }
        try:
            self.products.database.ping()
            checks["database"] = "ok"
            checks["catalog"] = "ok" if self.products.count() > 0 else "empty"
        except SQLAlchemyError:
            pass

        destination_ready = self.settings.webhook_enabled or self.settings.allow_store_only
        ready = (
            checks["database"] == "ok"
            and checks["catalog"] == "ok"
            and checks["configuration"] == "ok"
            and checks["invoice"] == "configured"
            and destination_ready
        )
        if not destination_ready:
            checks["destination"] = "required"
        return ReadinessResult(ready=ready, checks=checks)

    def _validate_runtime_limits(self, order) -> None:
        self._validate_items(order.items)

    def _validate_items(self, items) -> None:
        if len(items) > self.settings.max_order_items:
            raise ValidationAppError()
        total_boxes = 0
        for item in items:
            if item.boxes > self.settings.max_boxes_per_item:
                raise ValidationAppError()
            total_boxes += item.boxes
        if total_boxes > self.settings.max_total_boxes:
            raise ValidationAppError()

    @staticmethod
    def _to_draft(
        order: OrderCreate,
        *,
        selected_tariff: CdekTariffOptionResponse | None,
    ) -> OrderDraft:
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
            cdek_to_city_code=order.delivery.to_city_code,
            cdek_tariff_code=(
                selected_tariff.tariff_code if selected_tariff else None
            ),
            cdek_tariff_name=(
                selected_tariff.tariff_name if selected_tariff else None
            ),
            cdek_delivery_mode=(
                selected_tariff.delivery_mode if selected_tariff else None
            ),
            cdek_period_min_days=(
                selected_tariff.period_min_days if selected_tariff else None
            ),
            cdek_period_max_days=(
                selected_tariff.period_max_days if selected_tariff else None
            ),
            delivery_amount_kopecks=(
                selected_tariff.delivery_amount_kopecks if selected_tariff else 0
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
        except SQLAlchemyError:
            return "pending" if self.settings.webhook_enabled else "stored"
        if message is None:
            return "stored"
        return {
            "pending": "pending",
            "processing": "pending",
            "succeeded": "delivered",
            "failed": "failed",
        }.get(message.status, "pending")  # type: ignore[return-value]

    def _render_response(
        self,
        stored,
        *,
        integration_status: Literal["pending", "stored", "delivered", "failed"],
        replayed: bool,
    ) -> OrderResponse:
        items = [
            CalculatedItemResponse(
                sku=item.sku,
                name=item.product_name,
                boxes=item.boxes,
                units_per_box=item.units_per_box,
                units=item.units,
                price_per_unit_kopecks=item.price_per_unit_kopecks,
                price_per_box_kopecks=item.price_per_box_kopecks,
                line_amount_kopecks=item.line_amount_kopecks,
                weight_per_box_grams=item.unit_weight_grams,
                total_weight_grams=item.total_weight_grams,
                box_volume_mm3=item.unit_volume_mm3,
                length_mm=item.box_length_mm,
                width_mm=item.box_width_mm,
                height_mm=item.box_height_mm,
                cargo_places=item.cargo_places,
                total_volume_mm3=item.total_volume_mm3,
            )
            for item in stored.items
        ]
        totals = OrderCommercialTotalsResponse(
            total_boxes=stored.total_boxes,
            total_units=stored.total_units,
            products_amount_kopecks=stored.products_amount_kopecks,
            delivery_amount_kopecks=stored.delivery_amount_kopecks,
            grand_total_kopecks=stored.grand_total_kopecks,
            total_weight_grams=stored.total_weight_grams,
            cargo_places=stored.cargo_places,
            total_volume_mm3=stored.total_volume_mm3,
        )
        delivery = OrderDeliveryResponse(
            method=stored.delivery_method,
            type=stored.delivery_type,
            to_city_code=stored.cdek_to_city_code,
            tariff_code=stored.cdek_tariff_code,
            tariff_name=stored.cdek_tariff_name,
            delivery_mode=stored.cdek_delivery_mode,
            office_code=stored.delivery_office_code,
            region=stored.delivery_region,
            city=stored.delivery_city,
            postcode=stored.delivery_postcode,
            street=stored.delivery_street,
            house=stored.delivery_house,
            apartment=stored.delivery_apartment,
            period_min_days=stored.cdek_period_min_days,
            period_max_days=stored.cdek_period_max_days,
        )
        return OrderResponse(
            order_id=UUID(stored.order_id),
            order_number=stored.order_number,
            order_page_url=(
                f"{self.settings.customer_order_page_url}?"
                f"{urlencode({'number': stored.order_number})}"
            ),
            status="accepted",
            integration_status=integration_status,
            replayed=replayed,
            items=items,
            totals=totals,
            delivery=delivery,
        )


class OutboxProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        orders: OrderRepository,
        outbox: OutboxRepository,
        destination: OrderDestination | None,
        invoice_service: InvoiceService | None = None,
    ) -> None:
        self.settings = settings
        self.orders = orders
        self.outbox = outbox
        self.destination = destination
        self.invoice_service = invoice_service

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
            if message.event_type == "invoice.generate":
                try:
                    if self.invoice_service is None:
                        raise InvoiceGenerationFailedError()
                    self.invoice_service.ensure_invoice(message.order_id, now=now)
                except Exception:
                    if message.attempt_count < self.settings.webhook_max_attempts:
                        self.outbox.mark_retry(
                            message.id,
                            token,
                            "INVOICE_GENERATION_FAILED",
                            delay_seconds=self._retry_delay(message.attempt_count),
                            now=now,
                        )
                        retry_scheduled += 1
                    else:
                        self.outbox.mark_failed(
                            message.id,
                            token,
                            "INVOICE_GENERATION_FAILED",
                            now=now,
                        )
                        failed += 1
                else:
                    self.outbox.mark_success(message.id, token, now=now)
                    delivered += 1
                continue
            if message.event_type != "order.created" or self.destination is None:
                self.outbox.mark_failed(
                    message.id, token, "OUTBOX_EVENT_UNSUPPORTED", now=now
                )
                failed += 1
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
