from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")


def hmac_hex(secret: str, value: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def canonical_hmac(secret: str, value: Mapping[str, Any]) -> str:
    """Build a stable, one-way fingerprint without persisting customer data."""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hmac_hex(secret, canonical)


def is_valid_idempotency_key(value: str | None) -> bool:
    return bool(value and IDEMPOTENCY_KEY_PATTERN.fullmatch(value))


def resolve_client_ip(
    peer_ip: str | None,
    forwarded_for: str | None,
    trusted_proxies: Sequence[str],
) -> str:
    """Trust X-Forwarded-For only when the direct peer is explicitly trusted."""
    peer = _normalized_ip(peer_ip) or "unknown"
    if not forwarded_for or peer == "unknown":
        return peer

    if not _ip_is_trusted(peer, trusted_proxies):
        return peer

    forwarded_chain = [
        normalized
        for raw in forwarded_for.split(",")
        if (normalized := _normalized_ip(raw.strip())) is not None
    ]
    # Walk from the trusted direct peer towards the client. The first
    # non-trusted hop is authoritative; attacker-prepended leftmost values are
    # never selected while a real untrusted hop exists to their right.
    full_chain = [*forwarded_chain, peer]
    for hop in reversed(full_chain):
        if not _ip_is_trusted(hop, trusted_proxies):
            return hop
    return forwarded_chain[0] if forwarded_chain else peer


def _normalized_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _ip_is_trusted(value: str, trusted_proxies: Sequence[str]) -> bool:
    address = ipaddress.ip_address(value)
    for item in trusted_proxies:
        try:
            if "/" in item:
                if address in ipaddress.ip_network(item, strict=False):
                    return True
            elif address == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False
