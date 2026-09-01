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

    # Optional second provider, tried only when the primary fails in a way
    # another host could plausibly fix (timeout, unreachable). Empty disables
    # it entirely, which is the default: a fallback that nobody configured
    # should not quietly exist (ADR-0019).
    llm_fallback_provider: Literal["", "stub", "ollama", "openai_compatible"] = ""
    llm_fallback_model: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")

    # Which prompt version scores. None means "the highest-numbered file on
    # disk", which is the existing behaviour and stays the default.
    #
    # It is settable because that default has a sharp edge: prompt files are
    # immutable once committed, but "latest" is implicit, so **adding a file is
    # a deploy**. Writing prompts/match_score/v2.md to run an A/B changed what
    # the worker scores with, without a code change and without review. The
    # experiment won on the measurement (ADR-0021) so the promotion was the
    # right outcome — it should not have been an accident.
    llm_prompt_version: int | None = None

    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    # /metrics is unauthenticated by convention so Prometheus can scrape it, and
    # it leaks operational shape (queue depth, rejection reasons, tenant volume).
    # Off unless explicitly enabled, and documented as internal-network-only.
    metrics_enabled: bool = True

    # "local" keeps the strongest posture: worker-parse runs with
    # network_mode: none and reaches Postgres over a unix socket (ADR-0008).
    # "cloud" cannot: managed Postgres is TCP-only, so the parse worker needs a
    # network and the isolation guarantee is strictly weaker. Making this an
    # explicit mode rather than an emergent consequence of other settings means
    # the downgrade is a decision someone made, not something that happened.
    deployment_mode: Literal["local", "cloud"] = "local"

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
    def worker_parse_is_network_isolated(self) -> bool:
        """Whether the deployment CLAIMS the strongest isolation guarantee.

        Derived from deployment_mode alone, and deliberately so. The first
        version also checked `postgres_socket_dir` — but that is *this*
        process's setting, and the API does not use the socket, so a
        correctly-configured local stack reported `false`. A process cannot
        observe another container's network from its own environment.

        What actually enforces the guarantee is the compose file, and what
        verifies it is `test_worker_parse_declares_no_network`, which reads
        that file, plus the live probe recorded in ADR-0008. This property
        reports which mode is deployed; it is not evidence on its own.
        """
        return self.deployment_mode == "local"

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

    # Lets a webhook be pointed at a container on the compose network so the
    # relay can be demonstrated locally. It disables an SSRF control, so it is
    # refused outside dev by the validator below and reported by /readyz — an
    # operator must be able to see that this is off without taking it on faith.
    webhook_allow_private_destinations: bool = False

    @model_validator(mode="after")
    def _refuse_private_webhook_destinations_outside_dev(self) -> Settings:
        if self.webhook_allow_private_destinations and self.app_env != "dev":
            raise ValueError(
                f"Refusing to start with APP_ENV={self.app_env} and "
                f"WEBHOOK_ALLOW_PRIVATE_DESTINATIONS=true. That flag turns off the "
                f"SSRF check on tenant-supplied webhook URLs, which is what stops a "
                f"'webhook' pointed at the instance metadata service. It exists for "
                f"local demonstration only."
            )
        return self

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
