from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_application_uses_explicit_test_settings(app: FastAPI) -> None:
    settings = app.state.context.settings

    assert settings.app_env == "test"
    assert settings.debug is False
    assert settings.api_docs_enabled is False
    assert settings.database_url.startswith("sqlite+pysqlite:///")


def test_health_and_readiness_are_healthy_with_seeded_catalog(
    client: TestClient,
) -> None:
    health = client.get("/health")
    readiness = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ready",
        "checks": {
            "configuration": "ok",
            "database": "ok",
            "catalog": "ok",
            "destination": "store_only",
        },
    }


def test_readiness_fails_when_trusted_catalog_is_empty(
    app_factory: Callable[..., FastAPI],
) -> None:
    application = app_factory(seed_products=False)

    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["catalog"] == "empty"


def test_cors_preflight_allows_only_configured_origin(
    client: TestClient,
    allowed_origin: str,
) -> None:
    response = client.options(
        "/api/orders",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == allowed_origin
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "idempotency-key" in response.headers[
        "access-control-allow-headers"
    ].lower()
    assert response.headers.get("access-control-allow-credentials") != "true"


def test_cors_preflight_rejects_unconfigured_origin(client: TestClient) -> None:
    response = client.options(
        "/api/orders",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,idempotency-key",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
