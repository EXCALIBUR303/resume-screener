"""Build the gateway from settings — the one place a provider is chosen."""

from __future__ import annotations

import structlog

from screener_api.llm.gateway import LLMGateway
from screener_api.llm.provider import CircuitBreaker, LLMProvider, StubProvider, TokenBudget
from screener_api.llm.providers_live import OllamaProvider, OpenAICompatibleProvider
from screener_api.settings import Settings

log = structlog.get_logger()


def build_provider(settings: Settings) -> LLMProvider:
    match settings.llm_provider:
        case "stub":
            return StubProvider()
        case "ollama":
            return OllamaProvider(base_url=settings.llm_base_url, model_id=settings.llm_model)
        case "openai_compatible":
            return OpenAICompatibleProvider(
                base_url=settings.llm_base_url,
                model_id=settings.llm_model,
                api_key=settings.llm_api_key.get_secret_value(),
            )
    raise ValueError(f"unknown provider {settings.llm_provider!r}")


def build_gateway(settings: Settings) -> LLMGateway:
    provider = build_provider(settings)
    log.info("llm.provider_selected", provider=settings.llm_provider, model=provider.model_id)
    return LLMGateway(
        provider,
        budget=TokenBudget(max_tokens=settings.llm_max_monthly_tokens),
        breaker=CircuitBreaker(threshold=settings.llm_circuit_breaker_failures),
    )
