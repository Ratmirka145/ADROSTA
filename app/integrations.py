from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import threading
import time
from typing import Any, Callable, Mapping, Protocol

import httpx


logger = logging.getLogger("adrosta.cdek")

CDEK_BASE_URLS = {
    "test": "https://api.edu.cdek.ru/v2",
    "production": "https://api.cdek.ru/v2",
}
_TRANSIENT_STATUSES = frozenset({502, 503, 504})


class DestinationError(RuntimeError):
    """A safe integration error that never contains response bodies or tokens."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    delivered: bool
    upstream_status: int | None = None


class OrderDestination(Protocol):
    def send(
        self,
        *,
        order_id: str,
        payload: Mapping[str, Any],
    ) -> DeliveryReceipt: ...


class WebhookDestination:
    """Opt-in generic webhook; no CRM-specific contract is assumed."""

    def __init__(
        self,
        *,
        url: str,
        token: str | None,
        timeout_seconds: float,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_seconds
        self._client = client

    def send(
        self,
        *,
        order_id: str,
        payload: Mapping[str, Any],
    ) -> DeliveryReceipt:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": order_id,
            "User-Agent": "ADROSTA-Orders/1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            if self._client is not None:
                response = self._client.post(
                    self._url,
                    json=dict(payload),
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self._url,
                        json=dict(payload),
                        headers=headers,
                    )
        except (httpx.TimeoutException, httpx.NetworkError):
            raise DestinationError(
                "DESTINATION_UNAVAILABLE",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise DestinationError(
                "DESTINATION_HTTP_ERROR",
                retryable=True,
            ) from None

        status = response.status_code
        if 200 <= status < 300:
            return DeliveryReceipt(delivered=True, upstream_status=status)
        if status in {408, 425, 429} or status >= 500:
            raise DestinationError(
                "DESTINATION_TEMPORARY_FAILURE",
                retryable=True,
            )
        raise DestinationError(
            "DESTINATION_REJECTED",
            retryable=False,
        )


class CdekClientError(RuntimeError):
    """Safe transport failure that contains no upstream body or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CdekAuthError(CdekClientError):
    pass


class CdekUnavailableError(CdekClientError):
    pass


class CdekTimeoutError(CdekClientError):
    pass


class CdekBadResponseError(CdekClientError):
    pass


class CdekApi(Protocol):
    def cities(
        self, *, query: str, country_code: str, request_id: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    def delivery_points(
        self, *, city_code: int, request_id: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    def tariff_list(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> list[Mapping[str, Any]]: ...


class CdekClient:
    """Thin synchronous adapter for the official CDEK API v2."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        environment: str,
        timeout_seconds: float,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            self._base_url = CDEK_BASE_URLS[environment]
        except KeyError:
            raise ValueError("unsupported CDEK environment") from None
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._client = client or httpx.Client()
        self._owns_client = client is None
        self._clock = clock
        self._token_lock = threading.Lock()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def cities(
        self, *, query: str, country_code: str, request_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        payload = self._authorized_json(
            "GET",
            "/location/cities",
            operation="cities",
            request_id=request_id,
            params={"city": query, "country_codes": country_code, "size": 100},
        )
        return self._mapping_list(payload)

    def delivery_points(
        self, *, city_code: int, request_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        payload = self._authorized_json(
            "GET",
            "/deliverypoints",
            operation="delivery_points",
            request_id=request_id,
            params={"city_code": city_code, "type": "PVZ"},
        )
        return self._mapping_list(payload)

    def tariff_list(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        response_payload = self._authorized_json(
            "POST",
            "/calculator/tarifflist",
            operation="tariff_list",
            request_id=request_id,
            json=dict(payload),
        )
        if not isinstance(response_payload, Mapping):
            raise CdekBadResponseError("CDEK_BAD_RESPONSE")
        tariffs = response_payload.get("tariff_codes")
        result = self._mapping_list(tariffs)
        logger.info(
            "cdek_result operation=tariff_list request_id=%s count=%s",
            request_id or "-",
            len(result),
        )
        return result

    @staticmethod
    def _mapping_list(payload: Any) -> list[Mapping[str, Any]]:
        if not isinstance(payload, list) or any(
            not isinstance(item, Mapping) for item in payload
        ):
            raise CdekBadResponseError("CDEK_BAD_RESPONSE")
        return payload

    def _authorized_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        request_id: str | None,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        token = self._get_token()
        response = self._request(
            method,
            path,
            operation=operation,
            request_id=request_id,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json,
        )
        if response.status_code == 401:
            token = self._get_token(rejected_token=token)
            response = self._request(
                method,
                path,
                operation=operation,
                request_id=request_id,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )
            if response.status_code == 401:
                raise CdekAuthError("CDEK_AUTH_ERROR")
        if response.status_code in _TRANSIENT_STATUSES or response.status_code >= 500:
            raise CdekUnavailableError("CDEK_UNAVAILABLE")
        if not 200 <= response.status_code < 300:
            raise CdekBadResponseError("CDEK_BAD_RESPONSE")
        try:
            return response.json()
        except ValueError:
            raise CdekBadResponseError("CDEK_BAD_RESPONSE") from None

    def _get_token(self, *, rejected_token: str | None = None) -> str:
        now = self._clock()
        if (
            rejected_token is None
            and self._access_token
            and now < self._token_expires_at
        ):
            return self._access_token

        with self._token_lock:
            now = self._clock()
            if self._access_token and now < self._token_expires_at:
                if rejected_token is None or self._access_token != rejected_token:
                    return self._access_token

            response = self._request(
                "POST",
                "/oauth/token",
                operation="oauth",
                request_id=None,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if response.status_code in {400, 401, 403}:
                raise CdekAuthError("CDEK_AUTH_ERROR")
            if response.status_code in _TRANSIENT_STATUSES or response.status_code >= 500:
                raise CdekUnavailableError("CDEK_UNAVAILABLE")
            if not 200 <= response.status_code < 300:
                raise CdekBadResponseError("CDEK_BAD_RESPONSE")
            try:
                payload = response.json()
                token = payload["access_token"]
                expires_in = float(payload["expires_in"])
            except (ValueError, TypeError, KeyError, OverflowError):
                raise CdekBadResponseError("CDEK_BAD_RESPONSE") from None
            if (
                not isinstance(token, str)
                or not token
                or not math.isfinite(expires_in)
                or expires_in <= 0
            ):
                raise CdekBadResponseError("CDEK_BAD_RESPONSE")

            safety_margin = min(30.0, max(1.0, expires_in * 0.1))
            self._access_token = token
            self._token_expires_at = now + max(0.0, expires_in - safety_margin)
            return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        request_id: str | None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        started = self._clock()
        for attempt in range(2):
            try:
                response = self._client.request(
                    method,
                    self._base_url + path,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "ADROSTA-CDEK/1.0",
                        **dict(headers or {}),
                    },
                    params=params,
                    json=json,
                    data=data,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                if attempt == 0:
                    continue
                raise CdekTimeoutError("CDEK_TIMEOUT") from None
            except (httpx.NetworkError, httpx.HTTPError):
                if attempt == 0:
                    continue
                raise CdekUnavailableError("CDEK_UNAVAILABLE") from None

            if response.status_code in _TRANSIENT_STATUSES and attempt == 0:
                continue
            logger.info(
                "cdek_request operation=%s request_id=%s status=%s duration_ms=%s",
                operation,
                request_id or "-",
                response.status_code,
                round((self._clock() - started) * 1000),
            )
            return response
        raise CdekUnavailableError("CDEK_UNAVAILABLE")
