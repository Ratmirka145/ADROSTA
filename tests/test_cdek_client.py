from __future__ import annotations

from urllib.parse import parse_qs
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import httpx
import pytest

from app.integrations import (
    CdekAuthError,
    CdekClient,
    CdekTimeoutError,
    CdekUnavailableError,
)


def _client(handler, *, clock=lambda: 0.0) -> CdekClient:
    return CdekClient(
        client_id="unit-test-client",
        client_secret="unit-test-secret",
        environment="test",
        timeout_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=clock,
    )


def test_token_is_cached_between_authorized_requests() -> None:
    oauth_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal oauth_calls, api_calls
        if request.url.path.endswith("/oauth/token"):
            oauth_calls += 1
            form = parse_qs(request.content.decode())
            assert form == {
                "grant_type": ["client_credentials"],
                "client_id": ["unit-test-client"],
                "client_secret": ["unit-test-secret"],
            }
            return httpx.Response(
                200, json={"access_token": "cached-token", "expires_in": 3600}
            )
        api_calls += 1
        assert request.headers["authorization"] == "Bearer cached-token"
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.cities(query="Москва", country_code="RU")
    client.cities(query="Москва", country_code="RU")

    assert oauth_calls == 1
    assert api_calls == 2


def test_concurrent_requests_share_one_token_refresh() -> None:
    oauth_calls = 0
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal oauth_calls
        if request.url.path.endswith("/oauth/token"):
            with lock:
                oauth_calls += 1
            time.sleep(0.02)
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json=[])

    client = _client(handler)
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(
            executor.map(
                lambda _: client.cities(query="Москва", country_code="RU"),
                range(10),
            )
        )

    assert results == [[]] * 10
    assert oauth_calls == 1


@pytest.mark.parametrize(
    ("environment", "expected_host"),
    [("test", "api.edu.cdek.ru"), ("production", "api.cdek.ru")],
)
def test_environment_selects_only_official_host(environment, expected_host) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        return httpx.Response(200, json=[])

    client = CdekClient(
        client_id="unit-test-client",
        client_secret="unit-test-secret",
        environment=environment,
        timeout_seconds=1.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.cities(query="Москва", country_code="RU")
    assert hosts == [expected_host, expected_host]


def test_expired_token_is_refreshed() -> None:
    now = [0.0]
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            token = f"token-{len(tokens) + 1}"
            tokens.append(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 100})
        return httpx.Response(200, json=[])

    client = _client(handler, clock=lambda: now[0])
    client.cities(query="Москва", country_code="RU")
    now[0] = 91.0
    client.cities(query="Москва", country_code="RU")

    assert tokens == ["token-1", "token-2"]


def test_unauthorized_response_refreshes_and_retries_exactly_once() -> None:
    oauth_calls = 0
    api_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal oauth_calls
        if request.url.path.endswith("/oauth/token"):
            oauth_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{oauth_calls}", "expires_in": 3600},
            )
        api_authorizations.append(request.headers["authorization"])
        if len(api_authorizations) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[])

    client = _client(handler)
    client.cities(query="Москва", country_code="RU")

    assert oauth_calls == 2
    assert api_authorizations == ["Bearer token-1", "Bearer token-2"]


def test_second_unauthorized_response_is_not_retried_again() -> None:
    oauth_calls = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal oauth_calls, api_calls
        if request.url.path.endswith("/oauth/token"):
            oauth_calls += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{oauth_calls}", "expires_in": 3600},
            )
        api_calls += 1
        return httpx.Response(401)

    with pytest.raises(CdekAuthError):
        _client(handler).cities(query="Москва", country_code="RU")
    assert oauth_calls == 2
    assert api_calls == 2


def test_transient_5xx_is_retried_only_once() -> None:
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_calls
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        api_calls += 1
        return httpx.Response(503)

    with pytest.raises(CdekUnavailableError):
        _client(handler).cities(query="Москва", country_code="RU")
    assert api_calls == 2


def test_timeout_is_retried_once_then_normalized() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("safe unit-test timeout", request=request)

    with pytest.raises(CdekTimeoutError):
        _client(handler).cities(query="Москва", country_code="RU")
    assert calls == 2
