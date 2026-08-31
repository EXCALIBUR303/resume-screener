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
