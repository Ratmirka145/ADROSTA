from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings, get_settings
from app.container import ApplicationContext
from app.errors import install_error_handlers
from app.observability import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    configure_logging,
)
from app.rate_limit_middleware import OrderRateLimitMiddleware
from app.routers.cart import router as cart_router
from app.routers.cdek import router as cdek_router
from app.routers.customer import router as customer_router
from app.routers.health import router as health_router
from app.routers.orders import router as orders_router


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    context = ApplicationContext.build(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            context.close()

    application = FastAPI(
        title="ADROSTA API",
        version="0.2.0",
        description="Backend формы оптового заказа ADROSTA.",
        debug=runtime_settings.debug,
        docs_url="/docs" if runtime_settings.api_docs_enabled else None,
        redoc_url="/redoc" if runtime_settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if runtime_settings.api_docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.context = context

    if runtime_settings.allowed_hosts:
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(runtime_settings.allowed_hosts),
        )
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=runtime_settings.max_request_body_bytes,
    )
    application.add_middleware(OrderRateLimitMiddleware)
    if runtime_settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(runtime_settings.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=[
                "Idempotency-Replayed",
                "Retry-After",
                "X-Request-ID",
            ],
            max_age=600,
        )
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(health_router)
    application.include_router(cart_router)
    application.include_router(cdek_router)
    application.include_router(customer_router)
    application.include_router(orders_router)

    @application.get("/", tags=["system"])
    async def root():
        return {
            "service": "ADROSTA API",
            "status": "ok",
        }

    return application


app = create_app()
