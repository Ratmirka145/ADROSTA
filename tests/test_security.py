from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.security import resolve_client_ip


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def test_streamed_body_limit_cannot_be_bypassed_and_keeps_cors(
    app_factory: Callable[..., FastAPI],
    allowed_origin: str,
) -> None:
    application = app_factory(MAX_REQUEST_BODY_BYTES=128)

    def oversized_chunks():
        yield b'{"buyer":"'
        yield b"x" * 256
        yield b'"}'

    with TestClient(application) as client:
        response = client.post(
            "/api/orders",
            content=oversized_chunks(),
            headers={
                "Content-Type": "application/json",
                "Origin": allowed_origin,
                "Idempotency-Key": "oversized-order-key-0001",
            },
        )

    assert response.status_code == 413
    assert _error_code(response) == "PAYLOAD_TOO_LARGE"
    assert response.headers["access-control-allow-origin"] == allowed_origin
    assert response.headers["x-request-id"]


def test_invalid_attempts_are_eventually_hard_rate_limited(
    app_factory: Callable[..., FastAPI],
    allowed_origin: str,
) -> None:
    application = app_factory(ORDER_RATE_LIMIT_COUNT=2)
    headers = {"Origin": allowed_origin}
    with TestClient(application) as client:
        first = client.post("/api/orders", json={}, headers=headers)
        second = client.post("/api/orders", json={}, headers=headers)
        third = client.post("/api/orders", json={}, headers=headers)

    assert first.status_code == 422
    assert second.status_code == 422
    assert third.status_code == 429
    assert _error_code(third) == "RATE_LIMITED"
    assert third.headers["access-control-allow-origin"] == allowed_origin
    assert "retry-after" in third.headers["access-control-expose-headers"].lower()


def test_idempotent_replay_remains_available_after_rate_limit(
    app_factory: Callable[..., FastAPI],
    order_payload_factory,
) -> None:
    application = app_factory(ORDER_RATE_LIMIT_COUNT=1)
    payload = order_payload_factory()
    headers = {"Idempotency-Key": "replay-after-limit-key-0001"}

    with TestClient(application) as client:
        first = client.post("/api/orders", json=payload, headers=headers)
        replay = client.post("/api/orders", json=payload, headers=headers)
        another_payload = order_payload_factory()
        another_payload["comment"] = "Новый заказ"
        another = client.post(
            "/api/orders",
            json=another_payload,
            headers={"Idempotency-Key": "new-after-limit-key-0002"},
        )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["orderId"] == first.json()["orderId"]
    assert another.status_code == 429


def test_forwarded_for_ignores_attacker_prepended_address() -> None:
    resolved = resolve_client_ip(
        "10.0.0.2",
        "203.0.113.99, 198.51.100.7",
        ("10.0.0.0/8",),
    )
    assert resolved == "198.51.100.7"


def _production_env(tmp_path: Path) -> dict[str, str]:
    return {
        "APP_ENV": "production",
        "DEBUG": "false",
        "API_DOCS_ENABLED": "false",
        "DATABASE_PATH": str((tmp_path / "production.sqlite3").resolve()),
        "CORS_ALLOWED_ORIGINS": "https://shop.example.test",
        "ALLOWED_HOSTS": "api.example.test",
        "APP_HASH_SECRET": "a-production-secret-with-more-than-32-characters",
        "ALLOW_STORE_ONLY": "true",
    }


def test_production_rejects_insecure_origin(tmp_path: Path) -> None:
    environ = _production_env(tmp_path)
    environ["CORS_ALLOWED_ORIGINS"] = "http://shop.example.test"
    with pytest.raises(ConfigError):
        Settings.from_env(environ, load_env_file=False)


def test_allowed_hosts_rejects_port(tmp_path: Path) -> None:
    environ = _production_env(tmp_path)
    environ["ALLOWED_HOSTS"] = "api.example.test:443"
    with pytest.raises(ConfigError):
        Settings.from_env(environ, load_env_file=False)


@pytest.mark.parametrize(
    "url",
    [
        "https://sink.example.test/orders?api_key=must-not-be-in-url",
        "https://sink.example.test:notaport/orders",
    ],
)
def test_webhook_rejects_query_secrets_and_invalid_ports(
    tmp_path: Path,
    url: str,
) -> None:
    environ = _production_env(tmp_path)
    environ.update(
        {
            "WEBHOOK_ENABLED": "true",
            "ORDER_WEBHOOK_URL": url,
            "ORDER_WEBHOOK_TOKEN": "server-side-token",
        }
    )
    with pytest.raises(ConfigError):
        Settings.from_env(environ, load_env_file=False)


def test_settings_repr_does_not_expose_secrets(tmp_path: Path) -> None:
    environ = _production_env(tmp_path)
    settings = Settings.from_env(environ, load_env_file=False)
    assert environ["APP_HASH_SECRET"] not in repr(settings)
