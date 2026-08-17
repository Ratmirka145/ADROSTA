from __future__ import annotations

from dataclasses import dataclass

from app.cdek import CdekService
from app.config import Settings
from app.database import Database
from app.integrations import CdekClient, WebhookDestination
from app.invoice import InvoicePdfRenderer
from app.repositories import (
    CustomerSessionRepository,
    InvoiceRepository,
    OrderRepository,
    OutboxRepository,
    ProductRepository,
    RateLimitRepository,
)
from app.services import (
    CustomerAccessService,
    InvoiceService,
    OrderService,
    OutboxProcessor,
)


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    database: Database
    products: ProductRepository
    orders: OrderRepository | None
    outbox: OutboxRepository
    invoices: InvoiceRepository
    invoice_service: InvoiceService
    customer_sessions: CustomerSessionRepository
    customer_access_service: CustomerAccessService
    rate_limits: RateLimitRepository | None
    order_service: OrderService
    cdek_service: CdekService
    cdek_client: CdekClient | None

    @classmethod
    def build(cls, settings: Settings) -> "ApplicationContext":
        database = Database(settings.database_url)
        products = ProductRepository(database)
        outbox = OutboxRepository(database)
        invoices = InvoiceRepository(database)
        customer_sessions = CustomerSessionRepository(database)
        customer_access_service = CustomerAccessService(customer_sessions)
        invoice_service = InvoiceService(
            settings=settings,
            invoices=invoices,
            renderer=InvoicePdfRenderer(settings.invoice_font_path),
        )

        orders: OrderRepository | None = None
        rate_limits: RateLimitRepository | None = None
        if settings.app_hash_secret:
            orders = OrderRepository(
                database,
                settings.app_hash_secret,
                idempotency_ttl_seconds=settings.idempotency_ttl_seconds,
                duplicate_window_seconds=settings.duplicate_window_seconds,
                invoices=invoices,
                customer_sessions=customer_sessions,
            )
            rate_limits = RateLimitRepository(database, settings.app_hash_secret)

        service = OrderService(
            settings=settings,
            products=products,
            orders=orders,
            outbox=outbox,
            rate_limits=rate_limits,
            invoice_service=invoice_service,
            customer_sessions=customer_sessions,
        )
        cdek_client = None
        if settings.cdek_client_id and settings.cdek_client_secret:
            cdek_client = CdekClient(
                client_id=settings.cdek_client_id,
                client_secret=settings.cdek_client_secret,
                environment=settings.cdek_env,
                timeout_seconds=settings.cdek_http_timeout_seconds,
            )
        cdek_service = CdekService(
            client=cdek_client,
            from_city_code=settings.cdek_from_city_code,
            origin_mode=settings.cdek_origin_mode,
            calculate_items=service.calculate_items,
        )
        service.attach_cdek_service(cdek_service)
        return cls(
            settings=settings,
            database=database,
            products=products,
            orders=orders,
            outbox=outbox,
            invoices=invoices,
            invoice_service=invoice_service,
            customer_sessions=customer_sessions,
            customer_access_service=customer_access_service,
            rate_limits=rate_limits,
            order_service=service,
            cdek_service=cdek_service,
            cdek_client=cdek_client,
        )

    def outbox_processor(self) -> OutboxProcessor:
        if self.orders is None:
            raise RuntimeError("APP_HASH_SECRET is not configured")
        destination = None
        if self.settings.webhook_enabled and self.settings.webhook_url:
            destination = WebhookDestination(
                url=self.settings.webhook_url,
                token=self.settings.webhook_token,
                timeout_seconds=self.settings.webhook_timeout_seconds,
            )
        return OutboxProcessor(
            settings=self.settings,
            orders=self.orders,
            outbox=self.outbox,
            destination=destination,
            invoice_service=self.invoice_service,
        )

    def close(self) -> None:
        if self.cdek_client is not None:
            self.cdek_client.close()
        self.database.close()
