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


def test_env_example_produces_a_valid_settings_object() -> None:
    """`.env.example` is the file every new developer copies, and nothing checked it.

    Two of my own additions broke it, and CI found out by failing to start
    every container in the stack:

    * `LLM_FALLBACK_PROVIDER=   # "", stub, ollama, ...` — a quote character
      inside an inline comment stops both Compose's env_file parser and
      pydantic-settings from recognising it as a comment, so the whole
      `# ...` string became the value and failed the Literal.
    * `LLM_PROMPT_VERSION=` — `KEY=` parses as the empty string rather than as
      unset, and `""` is not an `int | None`.

    A cold start in CI catches this three minutes and a whole stack later. This
    catches it in milliseconds, which is the difference between finding it
    before pushing and finding it after.

    It is not a replacement for that cold start. This exercises
    *pydantic-settings*' parser; the containers are fed by *Compose*'s, and they
    are different implementations that merely agreed about this case. CI stays
    the authority on whether the stack boots.
    """
    import pathlib

    from screener_api.settings import Settings

    example = pathlib.Path(__file__).resolve().parents[3] / ".env.example"
    assert example.is_file(), "the environment contract is missing"

    settings = Settings(_env_file=str(example), _env_file_encoding="utf-8")

    # Spot-check values whose comment sits on the same line, since that is the
    # shape that goes wrong: a stray comment shows up as part of the value.
    assert settings.app_env == "dev"
    assert settings.llm_model == "qwen3:8b"
    assert "#" not in str(settings.storage_local_path)
    assert settings.llm_api_key.get_secret_value() == ""
    assert settings.llm_fallback_provider == ""
    assert settings.llm_prompt_version is None


def test_no_empty_value_carries_an_inline_comment() -> None:
    """The exact shape that breaks the parser, pinned so it cannot come back.

    `KEY=value  # note` strips fine. `KEY=  # note` does not: with nothing
    before the `#`, neither Compose's env_file parser nor pydantic-settings
    treats it as a comment, and the whole `# ...` string becomes the value.

    My first version of this test blamed the quote characters in my comment.
    That was wrong, and the test proved it by failing on
    `APP_ENV=dev  # ... Anything but "dev" ...`, which is fine. It also caught
    a pre-existing one: `LLM_API_KEY=` had carried its own comment as its value
    since M6 — invisible with Ollama, which ignores the key, and an
    `Authorization: Bearer # Only for openai_compatible...` header the moment
    anyone switched provider.
    """
    import pathlib
    import re

    example = pathlib.Path(__file__).resolve().parents[3] / ".env.example"
    offenders = [
        line
        for line in example.read_text().splitlines()
        if re.match(r"^[A-Z_][A-Z0-9_]*=[ \t]*#", line)
    ]
    assert not offenders, (
        "an inline comment after an empty value becomes the value; move it to "
        "its own line: " + "; ".join(offenders)
    )
