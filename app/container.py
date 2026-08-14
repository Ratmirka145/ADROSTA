from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.database import Database
from app.integrations import WebhookDestination
from app.repositories import (
    OrderRepository,
    OutboxRepository,
    ProductRepository,
    RateLimitRepository,
)
from app.services import OrderService, OutboxProcessor


@dataclass(slots=True)
class ApplicationContext:
    settings: Settings
    database: Database
    products: ProductRepository
    orders: OrderRepository | None
    outbox: OutboxRepository
    rate_limits: RateLimitRepository | None
    order_service: OrderService

    @classmethod
    def build(cls, settings: Settings) -> "ApplicationContext":
        database = Database(settings.database_url)
        products = ProductRepository(database)
        outbox = OutboxRepository(database)

        orders: OrderRepository | None = None
        rate_limits: RateLimitRepository | None = None
        if settings.app_hash_secret:
            orders = OrderRepository(
                database,
                settings.app_hash_secret,
                idempotency_ttl_seconds=settings.idempotency_ttl_seconds,
                duplicate_window_seconds=settings.duplicate_window_seconds,
            )
            rate_limits = RateLimitRepository(database, settings.app_hash_secret)

        service = OrderService(
            settings=settings,
            products=products,
            orders=orders,
            outbox=outbox,
            rate_limits=rate_limits,
        )
        return cls(
            settings=settings,
            database=database,
            products=products,
            orders=orders,
            outbox=outbox,
            rate_limits=rate_limits,
            order_service=service,
        )

    def outbox_processor(self) -> OutboxProcessor:
        if not self.settings.webhook_enabled or not self.settings.webhook_url:
            raise RuntimeError("Webhook delivery is not configured")
        if self.orders is None:
            raise RuntimeError("APP_HASH_SECRET is not configured")
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
        )

    def close(self) -> None:
        self.database.close()
