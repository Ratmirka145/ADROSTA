from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

import httpx


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
