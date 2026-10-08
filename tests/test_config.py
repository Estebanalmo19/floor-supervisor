import pytest

from src.config import load_settings
from src.exceptions import ConfigError

VALID_ENV = {
    "APP_ENV": "development",
    "APP_TIMEZONE": "America/Bogota",
    "DEVICE_ID": "FLOOR_SUPERVISOR_TABLET_01",
    "DUPLICATE_SCAN_WINDOW_SECONDS": "2",
    "LOG_LEVEL": "info",
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5433",
    "DB_NAME": "arrise_vm_db",
    "DB_USER": "floor_supervisor_app",
    "DB_PASSWORD": "db-pass-value",
    "DB_SSLMODE": "require",
    "CARD_RESOLVER_URL": "https://resolver.test/card-resolver/api/v1/cards/resolve",
    "CARD_RESOLVER_BEARER_TOKEN": "token-value",
    "CARD_RESOLVER_TIMEOUT_SECONDS": "5",
}


def test_valid_settings():
    settings = load_settings(VALID_ENV)
    assert settings.database.port == 5433
    assert settings.database.statement_timeout_ms == 5000
    assert settings.duplicate_scan_window_seconds == 2
    assert settings.card_resolver.timeout_seconds == 5.0
    assert settings.timezone.key == "America/Bogota"
    assert settings.log_level == "INFO"


def test_secrets_are_not_in_repr():
    settings = load_settings(VALID_ENV)
    text = repr(settings)
    assert "db-pass-value" not in text
    assert "token-value" not in text


def test_missing_values_are_all_reported_without_secret_values():
    env = {k: v for k, v in VALID_ENV.items() if k not in ("DB_PASSWORD", "DEVICE_ID")}
    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)
    message = str(exc_info.value)
    assert "DB_PASSWORD is required" in message
    assert "DEVICE_ID is required" in message
    assert "token-value" not in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_PORT", "abc"),
        ("DB_PORT", "70000"),
        ("DUPLICATE_SCAN_WINDOW_SECONDS", "-1"),
        ("DUPLICATE_SCAN_WINDOW_SECONDS", "61"),
        ("DEVICE_ID", "tablet 01|x"),
        ("APP_TIMEZONE", "Mars/Olympus"),
        ("CARD_RESOLVER_URL", "ftp://x"),
        ("CARD_RESOLVER_TIMEOUT_SECONDS", "0"),
        ("DB_SSLMODE", "maybe"),
        ("LOG_LEVEL", "LOUD"),
        ("DB_PASSWORD", "<secret>"),
        ("CARD_RESOLVER_BEARER_TOKEN", "<secret>"),
    ],
)
def test_invalid_values_rejected(key, value):
    with pytest.raises(ConfigError) as exc_info:
        load_settings({**VALID_ENV, key: value})
    assert key in str(exc_info.value)


# --- Power Automate ---------------------------------------------------------------------

SIGNED = "https://flow.example.test/workflows/x/triggers/manual/paths/invoke?api-version=1&sig=TOPSECRETSIG"


def test_power_automate_is_optional_and_disabled_without_url():
    settings = load_settings(VALID_ENV)
    assert settings.power_automate.url is None
    assert settings.power_automate.enabled is False
    assert settings.power_automate.timeout_seconds == 10.0


def test_power_automate_enabled_with_https_url_and_secret_hidden():
    settings = load_settings({**VALID_ENV, "POWER_AUTOMATE_URL": SIGNED, "POWER_AUTOMATE_TIMEOUT_SECONDS": "10"})
    assert settings.power_automate.enabled is True
    assert "TOPSECRETSIG" not in repr(settings)
    assert "TOPSECRETSIG" not in repr(settings.power_automate)


@pytest.mark.parametrize(("key", "value"), [
    ("POWER_AUTOMATE_URL", "http://flow.example.test/invoke?sig=TOPSECRETSIG"),
    ("POWER_AUTOMATE_TIMEOUT_SECONDS", "0"),
    ("POWER_AUTOMATE_TIMEOUT_SECONDS", "abc"),
    ("POWER_AUTOMATE_TIMEOUT_SECONDS", "61"),
])
def test_power_automate_invalid_values_rejected_without_leaking(key, value):
    with pytest.raises(ConfigError) as exc_info:
        load_settings({**VALID_ENV, key: value})
    assert key in str(exc_info.value)
    assert "TOPSECRETSIG" not in str(exc_info.value)
