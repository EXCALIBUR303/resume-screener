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
from pathlib import Path
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
    # Comma-separated in .env, parsed into a list by `cors_origins`. An
    # explicit allowlist, never "*": a wildcard with credentials is invalid,
    # and hands the API to any site the user happens to visit.
    cors_allowed_origins: str = "http://localhost:3000,https://localhost"
    app_name: str = "resume-screener"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    postgres_user: str = "screener"
    postgres_password: SecretStr = SecretStr("CHANGE_ME_dev_only")
    postgres_db: str = "screener"
    postgres_host: str = "db"
    # When set, connect over a unix socket instead of TCP. This is what lets
    # worker-parse run with network_mode: none and still reach Postgres.
    postgres_socket_dir: str = ""
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

    app_kek_version: int = 1

    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: Path = Path("/data/files")

    upload_max_bytes: int = 10 * 1024 * 1024
    upload_max_pages: int = 30
    upload_max_chars: int = 500_000
    zip_max_ratio: int = 100
    zip_max_entries: int = 2000
    zip_max_uncompressed_bytes: int = 100 * 1024 * 1024
    clamav_enabled: bool = False

    llm_provider: Literal["stub", "ollama", "openai_compatible"] = "ollama"
    llm_model: str = "qwen3:8b"
    llm_base_url: str = "http://host.docker.internal:11434"
    llm_api_key: SecretStr = SecretStr("")
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_seconds: int = 60
    # 0 = unlimited, which is safe for a local model. Set a real number before
    # ever pointing this at a paid endpoint.
    llm_max_monthly_tokens: int = 0
    llm_circuit_breaker_failures: int = 5

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    # /metrics is unauthenticated by convention so Prometheus can scrape it, and
    # it leaks operational shape (queue depth, rejection reasons, tenant volume).
    # Off unless explicitly enabled, and documented as internal-network-only.
    metrics_enabled: bool = True

    ocr_enabled: bool = True
    ocr_min_confidence: int = 60
    parse_timeout_seconds: int = 60
    parse_memory_limit_mb: int = 1024

    worker_poll_interval_ms: int = 500
    worker_max_attempts: int = 5
    worker_lease_timeout_seconds: int = 300
    worker_concurrency_per_org: int = 4

    # Deliberately a plain property, NOT a computed_field: a computed_field is
    # included in repr() and model_dump(), which would re-expose the password
    # that SecretStr exists to hide. Caught by test_secrets_do_not_leak_through_repr.
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def dsn(self) -> str:
        """The DSN the app actually connects with. Contains the password — never log it."""
        if self.database_url is not None:
            return str(self.database_url)
        password = self.postgres_password.get_secret_value()
        if self.postgres_socket_dir:
            return (
                f"postgresql+psycopg://{self.postgres_user}:{password}"
                f"@/{self.postgres_db}?host={self.postgres_socket_dir}"
            )
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}@"
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
