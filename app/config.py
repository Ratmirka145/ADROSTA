"""Application configuration loaded from environment variables.

Only non-secret development defaults live in this module.  Production settings
are validated eagerly so that the service cannot start with a wildcard CORS
policy, an ephemeral database, or without a key for privacy-preserving hashes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import ipaddress
import os
import re
from typing import Final
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})
_ENVIRONMENTS: Final = frozenset({"development", "test", "production"})
_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_HOST_RE: Final = re.compile(
    r"^(?:localhost|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*)$"
)
_UNSAFE_SECRET_VALUES: Final = frozenset(
    {
        "change-me",
        "changeme",
        "replace-me",
        "replace_with_a_random_secret",
        "secret",
        "your-secret-here",
    }
)


class ConfigError(RuntimeError):
    """Raised when environment configuration is invalid or unsafe."""


def _get(source: Mapping[str, str], name: str, default: str = "") -> str:
    value = source.get(name, default)
    return value.strip() if isinstance(value, str) else str(value).strip()


def _parse_bool(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(source, name)
    if not raw:
        return default
    normalized = raw.casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def _parse_int(
    source: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = _get(source, name)
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be at least {minimum}{upper}")
    return value


def _parse_float(
    source: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    raw = _get(source, name)
    try:
        value = float(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be at least {minimum}{upper}")
    return value


def _parse_csv(source: Mapping[str, str], name: str) -> tuple[str, ...]:
    """Parse a comma-separated env value, preserving order and removing duplicates."""

    raw = _get(source, name)
    if not raw:
        return ()
    values: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            raise ConfigError(f"{name} contains an empty list item")
        if value not in values:
            values.append(value)
    return tuple(values)


def _validate_origins(origins: tuple[str, ...], *, production: bool) -> None:
    for origin in origins:
        if "*" in origin:
            raise ConfigError("CORS_ALLOWED_ORIGINS must contain exact origins, not wildcards")
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in ({"https"} if production else {"http", "https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or origin.endswith("/")
        ):
            raise ConfigError(
                "CORS_ALLOWED_ORIGINS entries must be exact http(s) origins without "
                "credentials, path, query, fragment, or trailing slash"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigError("CORS_ALLOWED_ORIGINS contains an invalid port") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("CORS_ALLOWED_ORIGINS contains an invalid port")


def _validate_hosts(hosts: tuple[str, ...]) -> None:
    for host in hosts:
        if "*" in host:
            raise ConfigError("ALLOWED_HOSTS must contain exact hosts, not wildcards")
        candidate = host
        if candidate.startswith("["):
            raise ConfigError(
                "ALLOWED_HOSTS does not support IPv6 literals with this server version"
            )

        host_without_port = candidate
        if candidate.count(":") == 1:
            raise ConfigError("ALLOWED_HOSTS entries must not include ports")
        try:
            parsed_address = ipaddress.ip_address(host_without_port)
            if parsed_address.version == 6:
                raise ConfigError(
                    "ALLOWED_HOSTS does not support IPv6 literals with this server version"
                )
        except ValueError:
            if not _HOST_RE.fullmatch(candidate):
                raise ConfigError(
                    "ALLOWED_HOSTS entries must be exact host names or IP addresses"
                )


def _validate_proxy_ips(values: tuple[str, ...]) -> None:
    for value in values:
        if value == "*":
            raise ConfigError("TRUSTED_PROXY_IPS must not contain a wildcard")
        try:
            ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ConfigError(
                "TRUSTED_PROXY_IPS entries must be IP addresses or CIDR networks"
            ) from exc


def _validate_webhook_url(url: str, *, production: bool) -> None:
    parsed = urlsplit(url)
    allowed_schemes = {"https"} if production else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        scheme_description = "https" if production else "http(s)"
        raise ConfigError(
            f"ORDER_WEBHOOK_URL must be a valid {scheme_description} URL without "
            "embedded credentials, query parameters, or a fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("ORDER_WEBHOOK_URL contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("ORDER_WEBHOOK_URL contains an invalid port")


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed runtime settings. Secrets are intentionally excluded from repr."""

    app_env: str
    debug: bool
    api_docs_enabled: bool
    log_level: str

    database_url: str = field(repr=False)

    cors_allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    trusted_proxy_ips: tuple[str, ...]

    app_hash_secret: str | None = field(repr=False)
    idempotency_ttl_seconds: int
    duplicate_window_seconds: int
    idempotency_key_max_length: int

    order_rate_limit_count: int
    order_rate_limit_window_seconds: int

    max_request_body_bytes: int
    max_order_items: int
    max_boxes_per_item: int
    max_total_boxes: int

    webhook_enabled: bool
    webhook_url: str | None
    webhook_token: str | None = field(repr=False)
    webhook_timeout_seconds: float
    webhook_max_attempts: int
    webhook_backoff_seconds: float

    outbox_enabled: bool
    outbox_poll_interval_seconds: float
    outbox_batch_size: int
    outbox_lock_seconds: int
    allow_store_only: bool

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    def require_hash_secret(self) -> bytes:
        """Return the HMAC key or fail before privacy-sensitive hashing is attempted."""

        if not self.app_hash_secret:
            raise ConfigError("APP_HASH_SECRET is required for this operation")
        return self.app_hash_secret.encode("utf-8")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        load_env_file: bool = True,
    ) -> "Settings":
        if environ is None:
            if load_env_file:
                load_dotenv(override=False)
            source: Mapping[str, str] = os.environ
        else:
            source = environ

        app_env = _get(source, "APP_ENV", "development").casefold()
        if app_env not in _ENVIRONMENTS:
            raise ConfigError("APP_ENV must be development, test, or production")

        log_level = _get(source, "LOG_LEVEL", "INFO").upper()
        if log_level not in _LOG_LEVELS:
            raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

        database_url = _get(source, "DATABASE_URL")
        if not database_url:
            raise ConfigError("DATABASE_URL is required")
        try:
            parsed_database_url = make_url(database_url)
        except ArgumentError as exc:
            raise ConfigError("DATABASE_URL must be a valid SQLAlchemy URL") from exc
        driver_name = parsed_database_url.drivername
        if app_env == "test":
            allowed_database_drivers = {
                "postgresql+psycopg",
                "sqlite",
                "sqlite+pysqlite",
            }
        else:
            allowed_database_drivers = {"postgresql+psycopg"}
        if driver_name not in allowed_database_drivers:
            expected = "postgresql+psycopg"
            if app_env == "test":
                expected += " or sqlite+pysqlite"
            raise ConfigError(f"DATABASE_URL driver must be {expected}")

        origins = _parse_csv(source, "CORS_ALLOWED_ORIGINS")
        hosts = _parse_csv(source, "ALLOWED_HOSTS")
        proxies = _parse_csv(source, "TRUSTED_PROXY_IPS")
        _validate_origins(origins, production=app_env == "production")
        _validate_hosts(hosts)
        _validate_proxy_ips(proxies)

        hash_secret = _get(source, "APP_HASH_SECRET") or None
        webhook_enabled = _parse_bool(source, "WEBHOOK_ENABLED", False)
        webhook_url = _get(source, "ORDER_WEBHOOK_URL") or None
        webhook_token = _get(source, "ORDER_WEBHOOK_TOKEN") or None
        outbox_enabled = _parse_bool(source, "OUTBOX_ENABLED", True)
        allow_store_only = _parse_bool(source, "ALLOW_STORE_ONLY", app_env != "production")

        if webhook_enabled and not webhook_url:
            raise ConfigError("ORDER_WEBHOOK_URL is required when WEBHOOK_ENABLED=true")
        if webhook_url:
            _validate_webhook_url(webhook_url, production=app_env == "production")
        if webhook_enabled and not outbox_enabled:
            raise ConfigError("OUTBOX_ENABLED must be true when WEBHOOK_ENABLED=true")

        settings = cls(
            app_env=app_env,
            debug=_parse_bool(source, "DEBUG", app_env == "development"),
            api_docs_enabled=_parse_bool(
                source, "API_DOCS_ENABLED", app_env != "production"
            ),
            log_level=log_level,
            database_url=database_url,
            cors_allowed_origins=origins,
            allowed_hosts=hosts,
            trusted_proxy_ips=proxies,
            app_hash_secret=hash_secret,
            idempotency_ttl_seconds=_parse_int(
                source, "IDEMPOTENCY_TTL_SECONDS", 86_400, maximum=2_592_000
            ),
            duplicate_window_seconds=_parse_int(
                source,
                "DUPLICATE_WINDOW_SECONDS",
                300,
                minimum=1,
                maximum=86_400,
            ),
            idempotency_key_max_length=_parse_int(
                source, "IDEMPOTENCY_KEY_MAX_LENGTH", 128, minimum=16, maximum=200
            ),
            order_rate_limit_count=_parse_int(
                source, "ORDER_RATE_LIMIT_COUNT", 5, maximum=10_000
            ),
            order_rate_limit_window_seconds=_parse_int(
                source, "ORDER_RATE_LIMIT_WINDOW_SECONDS", 60, maximum=86_400
            ),
            max_request_body_bytes=_parse_int(
                source, "MAX_REQUEST_BODY_BYTES", 65_536, maximum=1_048_576
            ),
            max_order_items=_parse_int(
                source, "MAX_ORDER_ITEMS", 100, maximum=100
            ),
            max_boxes_per_item=_parse_int(
                source, "MAX_BOXES_PER_ITEM", 10_000, maximum=10_000
            ),
            max_total_boxes=_parse_int(
                source, "MAX_TOTAL_BOXES", 20_000, maximum=2_000_000
            ),
            webhook_enabled=webhook_enabled,
            webhook_url=webhook_url,
            webhook_token=webhook_token,
            webhook_timeout_seconds=_parse_float(
                source, "WEBHOOK_TIMEOUT_SECONDS", 5.0, minimum=0.1, maximum=60.0
            ),
            webhook_max_attempts=_parse_int(
                source, "WEBHOOK_MAX_ATTEMPTS", 5, maximum=100
            ),
            webhook_backoff_seconds=_parse_float(
                source, "WEBHOOK_BACKOFF_SECONDS", 5.0, minimum=0.1, maximum=3_600.0
            ),
            outbox_enabled=outbox_enabled,
            outbox_poll_interval_seconds=_parse_float(
                source,
                "OUTBOX_POLL_INTERVAL_SECONDS",
                2.0,
                minimum=0.1,
                maximum=300.0,
            ),
            outbox_batch_size=_parse_int(
                source, "OUTBOX_BATCH_SIZE", 20, maximum=1_000
            ),
            outbox_lock_seconds=_parse_int(
                source, "OUTBOX_LOCK_SECONDS", 60, maximum=3_600
            ),
            allow_store_only=allow_store_only,
        )
        settings._validate_cross_field_rules()
        return settings

    def _validate_cross_field_rules(self) -> None:
        if self.max_total_boxes < self.max_boxes_per_item:
            raise ConfigError(
                "MAX_TOTAL_BOXES must be greater than or equal to MAX_BOXES_PER_ITEM"
            )
        if (
            self.webhook_enabled
            and self.outbox_lock_seconds <= self.webhook_timeout_seconds + 5
        ):
            raise ConfigError(
                "OUTBOX_LOCK_SECONDS must exceed WEBHOOK_TIMEOUT_SECONDS by more than 5 seconds"
            )

        if not self.is_production:
            return

        if self.debug:
            raise ConfigError("DEBUG must be false in production")
        if self.api_docs_enabled:
            raise ConfigError("API_DOCS_ENABLED must be false in production")
        if not self.cors_allowed_origins:
            raise ConfigError("CORS_ALLOWED_ORIGINS is required in production")
        if not self.allowed_hosts:
            raise ConfigError("ALLOWED_HOSTS is required in production")
        if not self.app_hash_secret:
            raise ConfigError("APP_HASH_SECRET is required in production")
        if (
            len(self.app_hash_secret) < 32
            or self.app_hash_secret.casefold() in _UNSAFE_SECRET_VALUES
        ):
            raise ConfigError(
                "APP_HASH_SECRET must be at least 32 characters and not a placeholder"
            )
        if self.webhook_enabled and not self.webhook_token:
            raise ConfigError("ORDER_WEBHOOK_TOKEN is required for a production webhook")
        if not self.webhook_enabled and not self.allow_store_only:
            raise ConfigError(
                "Configure a webhook or explicitly set ALLOW_STORE_ONLY=true in production"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache process-wide settings."""

    return Settings.from_env()


def clear_settings_cache() -> None:
    """Clear cached settings; intended for tests that replace environment values."""

    get_settings.cache_clear()


__all__ = [
    "ConfigError",
    "Settings",
    "clear_settings_cache",
    "get_settings",
]
