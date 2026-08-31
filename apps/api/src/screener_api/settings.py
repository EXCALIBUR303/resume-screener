"""Application configuration.

Two rules this module exists to enforce:

1. Secrets are ``SecretStr`` so they cannot be printed, logged, or serialised by
   accident (repr is ``**********``).
2. The process refuses to start outside dev if any secret still holds its
   ``.env.example`` placeholder. A shipped default is the most common way a
   portfolio project quietly becomes exploitable.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Any secret whose value starts with one of these is a placeholder, not a secret.
PLACEHOLDER_PREFIXES = ("CHANGE_ME", "changeme", "your-", "xxx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env carries worker/web keys this process does not read
        case_sensitive=False,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "resume-screener"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    postgres_user: str = "screener"
    postgres_password: SecretStr = SecretStr("CHANGE_ME_dev_only")
    postgres_db: str = "screener"
    postgres_host: str = "db"
    postgres_port: int = 5432
    db_pool_size: int = 10
    db_statement_timeout_ms: int = 15_000

    # Optional override. Leave unset and the DSN is derived from the parts above —
    # a literal copy of the password here drifts the moment you rotate it.
    database_url: PostgresDsn | None = None

    app_kek: SecretStr = SecretStr("CHANGE_ME_base64_32_bytes")
    jwt_secret: SecretStr = SecretStr("CHANGE_ME_base64_32_bytes")
    jwt_algorithm: Literal["HS256"] = "HS256"  # pinned; a list here would allow alg confusion
    jwt_issuer: str = "resume-screener"
    jwt_audience: str = "resume-screener-api"
    access_token_ttl_seconds: int = 900  # 15 min
    refresh_token_ttl_seconds: int = 1_209_600  # 14 days, rotating
    auth_local_enabled: bool = True
    login_rate_limit_per_minute: int = 5

    upload_max_bytes: int = 10 * 1024 * 1024
    upload_max_pages: int = 30

    # Deliberately a plain property, NOT a computed_field: a computed_field is
    # included in repr() and model_dump(), which would re-expose the password
    # that SecretStr exists to hide. Caught by test_secrets_do_not_leak_through_repr.
    @property
    def dsn(self) -> str:
        """The DSN the app actually connects with. Contains the password — never log it."""
        if self.database_url is not None:
            return str(self.database_url)
        return (
            f"postgresql+psycopg://{self.postgres_user}:"
            f"{self.postgres_password.get_secret_value()}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    def placeholder_secrets(self) -> list[str]:
        """Names of secrets still holding an example-file placeholder."""
        found: list[str] = []
        for name in ("postgres_password", "app_kek", "jwt_secret"):
            value: SecretStr = getattr(self, name)
            if value.get_secret_value().startswith(PLACEHOLDER_PREFIXES):
                found.append(name.upper())
        return found

    @model_validator(mode="after")
    def _refuse_placeholder_secrets_outside_dev(self) -> Settings:
        if self.app_env == "dev":
            return self
        if bad := self.placeholder_secrets():
            raise ValueError(
                f"Refusing to start with APP_ENV={self.app_env}: "
                f"{', '.join(bad)} still hold their .env.example placeholder. "
                f"Generate real values with: openssl rand -base64 32"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
