"""Typed application configuration loaded from environment variables (.env in development)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from src.exceptions import ConfigError

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_DUPLICATE_WINDOW_SECONDS = 60
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    sslmode: str
    connect_timeout_seconds: int
    statement_timeout_ms: int


@dataclass(frozen=True)
class CardResolverSettings:
    url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float


@dataclass(frozen=True)
class PowerAutomateSettings:
    """Optional mirror to a Power Automate HTTP trigger. url=None disables it.

    The URL contains a signed ``sig`` query parameter: it is a secret.
    """

    url: str | None = field(repr=False)
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class Settings:
    app_env: str
    timezone: ZoneInfo
    device_id: str
    duplicate_scan_window_seconds: int
    log_level: str
    database: DatabaseSettings
    card_resolver: CardResolverSettings
    power_automate: PowerAutomateSettings


def load_settings(
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    """Build Settings from ``env`` (defaults to os.environ after loading .env).

    Raises ConfigError listing every problem found. Secret values never appear in messages.
    """
    if env is None:
        load_dotenv(dotenv_path=dotenv_path or Path.cwd() / ".env", override=False)
        env = os.environ

    reader = _EnvReader(env)

    database = DatabaseSettings(
        host=reader.required("DB_HOST"),
        port=reader.integer("DB_PORT", minimum=1, maximum=65535),
        name=reader.required("DB_NAME"),
        user=reader.required("DB_USER"),
        password=reader.required("DB_PASSWORD", secret=True),
        sslmode=reader.choice("DB_SSLMODE", _SSL_MODES, default="require"),
        connect_timeout_seconds=reader.integer(
            "DB_CONNECT_TIMEOUT_SECONDS", default=5, minimum=1, maximum=60
        ),
        statement_timeout_ms=reader.integer(
            "DB_STATEMENT_TIMEOUT_MS", default=5000, minimum=100, maximum=60000
        ),
    )
    card_resolver = CardResolverSettings(
        url=reader.url("CARD_RESOLVER_URL"),
        bearer_token=reader.required("CARD_RESOLVER_BEARER_TOKEN", secret=True),
        timeout_seconds=reader.number(
            "CARD_RESOLVER_TIMEOUT_SECONDS", default=5.0, minimum=0.5, maximum=30.0
        ),
    )
    power_automate = PowerAutomateSettings(
        url=reader.optional_secret_url("POWER_AUTOMATE_URL"),
        timeout_seconds=reader.number(
            "POWER_AUTOMATE_TIMEOUT_SECONDS", default=10.0, minimum=1.0, maximum=60.0
        ),
    )
    settings = Settings(
        app_env=reader.optional("APP_ENV", default="development"),
        timezone=reader.timezone("APP_TIMEZONE", default="America/Bogota"),
        device_id=reader.pattern("DEVICE_ID", DEVICE_ID_PATTERN),
        duplicate_scan_window_seconds=reader.integer(
            "DUPLICATE_SCAN_WINDOW_SECONDS",
            default=2,
            minimum=0,
            maximum=MAX_DUPLICATE_WINDOW_SECONDS,
        ),
        log_level=reader.choice("LOG_LEVEL", _LOG_LEVELS, default="INFO", upper=True),
        database=database,
        card_resolver=card_resolver,
        power_automate=power_automate,
    )

    if reader.errors:
        raise ConfigError("Invalid configuration: " + "; ".join(reader.errors))
    return settings


class _EnvReader:
    """Collects validation errors instead of failing on the first one."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def _get(self, key: str) -> str | None:
        value = self._env.get(key)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def optional(self, key: str, default: str) -> str:
        return self._get(key) or default

    def required(self, key: str, secret: bool = False) -> str:
        value = self._get(key)
        if value is None:
            self.errors.append(f"{key} is required")
            return ""
        if secret and value.startswith("<") and value.endswith(">"):
            self.errors.append(f"{key} still contains a placeholder value")
        return value

    def integer(
        self, key: str, minimum: int, maximum: int, default: int | None = None
    ) -> int:
        raw = self._get(key)
        if raw is None:
            if default is None:
                self.errors.append(f"{key} is required")
                return minimum
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{key} must be an integer")
            return minimum
        if not minimum <= value <= maximum:
            self.errors.append(f"{key} must be between {minimum} and {maximum}")
        return value

    def number(self, key: str, default: float, minimum: float, maximum: float) -> float:
        raw = self._get(key)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            self.errors.append(f"{key} must be a number")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{key} must be between {minimum} and {maximum}")
        return value

    def choice(
        self, key: str, allowed: set[str], default: str, upper: bool = False
    ) -> str:
        value = self._get(key) or default
        if upper:
            value = value.upper()
        if value not in allowed:
            self.errors.append(f"{key} must be one of {sorted(allowed)}")
        return value

    def pattern(self, key: str, regex: re.Pattern[str]) -> str:
        value = self.required(key)
        if value and not regex.fullmatch(value):
            self.errors.append(f"{key} has an invalid format")
        return value

    def url(self, key: str) -> str:
        value = self.required(key)
        if value and not value.startswith(("https://", "http://")):
            self.errors.append(f"{key} must be an http(s) URL")
        return value

    def optional_secret_url(self, key: str) -> str | None:
        """An optional https URL that is a secret: errors never include the value."""
        value = self._get(key)
        if value is None:
            return None
        if not value.startswith("https://"):
            self.errors.append(f"{key} must be an https URL")
        return value

    def timezone(self, key: str, default: str) -> ZoneInfo:
        name = self._get(key) or default
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            self.errors.append(f"{key} is not a valid IANA timezone")
            return ZoneInfo("UTC")
