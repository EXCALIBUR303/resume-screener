from __future__ import annotations

import pytest
from pydantic import ValidationError

from screener_api.settings import Settings


def _base(**overrides: object) -> dict[str, object]:
    return {
        "postgres_password": "real-password",
        "app_kek": "real-kek",
        "jwt_secret": "real-jwt",
        **overrides,
    }


@pytest.mark.parametrize("env", ["prod", "test"])
def test_placeholder_secrets_refuse_to_start_outside_dev(env: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(**_base(app_env=env, jwt_secret="CHANGE_ME_base64_32_bytes"))


def test_placeholder_secrets_are_allowed_in_dev() -> None:
    settings = Settings(**_base(app_env="dev", jwt_secret="CHANGE_ME_base64_32_bytes"))
    assert settings.placeholder_secrets() == ["JWT_SECRET"]


def test_real_secrets_start_in_prod() -> None:
    assert Settings(**_base(app_env="prod")).placeholder_secrets() == []


def test_dsn_is_derived_from_parts() -> None:
    settings = Settings(**_base(postgres_host="db", postgres_port=5432))
    assert settings.dsn == "postgresql+psycopg://screener:real-password@db:5432/screener"


def test_explicit_database_url_wins() -> None:
    """Set DATABASE_URL only to point at an external database, e.g. Neon."""
    settings = Settings(**_base(database_url="postgresql://u:p@neon.example:5432/d"))
    assert "neon.example" in settings.dsn


def test_secrets_do_not_leak_through_repr() -> None:
    """repr() lands in tracebacks and debug logs. It must never carry a secret —
    including indirectly, via a derived field like the DSN."""
    settings = Settings(**_base())
    assert "real-password" not in repr(settings)
    assert "real-kek" not in str(settings.app_kek)


def test_secrets_do_not_leak_through_model_dump() -> None:
    dumped = str(Settings(**_base()).model_dump())
    assert "real-password" not in dumped
    assert "real-jwt" not in dumped


def test_dsn_is_still_reachable_deliberately() -> None:
    """The password is available where it is actually needed, just not by accident."""
    assert "real-password" in Settings(**_base()).dsn


def test_the_webhook_ssrf_bypass_is_refused_outside_dev() -> None:
    """A flag that disables a security control must not be settable in prod.

    WEBHOOK_ALLOW_PRIVATE_DESTINATIONS turns off the address check on
    tenant-supplied webhook URLs — the check that stops a "webhook" pointed at
    the instance metadata service. It exists for local demonstration and the
    process refuses to boot with it set anywhere else (ADR-0018).
    """
    import pytest

    from screener_api.settings import Settings

    common = {
        "postgres_password": "real-value",
        "app_kek": "real-value",
        "jwt_secret": "real-value",
        "webhook_allow_private_destinations": True,
    }
    # Allowed in dev, which is the only place it is meant to be used.
    assert Settings(app_env="dev", **common).webhook_allow_private_destinations

    for env in ("test", "prod"):
        with pytest.raises(ValueError, match="WEBHOOK_ALLOW_PRIVATE_DESTINATIONS"):
            Settings(app_env=env, **common)


def test_readyz_reports_whether_the_ssrf_check_is_on() -> None:
    """A disabled control should be visible on the instance, not only in a
    .env file nobody reading /readyz can see."""
    from fastapi.testclient import TestClient

    from screener_api.main import create_app
    from screener_api.settings import Settings

    settings = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        webhook_allow_private_destinations=True,
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/readyz").json()
    assert body["webhook_ssrf_check_enabled"] is False
