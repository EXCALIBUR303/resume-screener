"""Build the gateway from settings — the one place a provider is chosen."""

from __future__ import annotations

import structlog

from screener_api.llm.gateway import LLMGateway
from screener_api.llm.provider import CircuitBreaker, LLMProvider, StubProvider, TokenBudget
from screener_api.llm.providers_live import OllamaProvider, OpenAICompatibleProvider
from screener_api.llm.router import Route, RoutedProvider
from screener_api.settings import Settings

log = structlog.get_logger()


def _provider(kind: str, *, model: str, base_url: str, api_key: str) -> LLMProvider:
    match kind:
        case "stub":
            return StubProvider()
        case "ollama":
            return OllamaProvider(base_url=base_url, model_id=model)
        case "openai_compatible":
            return OpenAICompatibleProvider(base_url=base_url, model_id=model, api_key=api_key)
    raise ValueError(f"unknown provider {kind!r}")


def build_provider(settings: Settings) -> LLMProvider:
    primary = _provider(
        settings.llm_provider,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
    )
    if not settings.llm_fallback_provider:
        return primary

    fallback = _provider(
        settings.llm_fallback_provider,
        model=settings.llm_fallback_model or settings.llm_model,
        base_url=settings.llm_fallback_base_url,
        api_key=settings.llm_fallback_api_key.get_secret_value(),
    )
    log.info(
        "llm.fallback_configured",
        primary=primary.model_id,
        fallback=fallback.model_id,
    )
    # Per-route breakers, not one shared: a shared breaker trips on the
    # primary's failures and then refuses the fallback, so the fallback is
    # unreachable exactly when it is needed.
    return RoutedProvider(
        routes=[
            Route(primary, CircuitBreaker(threshold=settings.llm_circuit_breaker_failures)),
            Route(fallback, CircuitBreaker(threshold=settings.llm_circuit_breaker_failures)),
        ]
    )


def build_gateway(settings: Settings) -> LLMGateway:
    provider = build_provider(settings)
    log.info("llm.provider_selected", provider=settings.llm_provider, model=provider.model_id)
    return LLMGateway(
        provider,
        budget=TokenBudget(max_tokens=settings.llm_max_monthly_tokens),
        # The gateway breaker still guards the whole call. When a router is in
        # place its per-route breakers open first, so this one only trips when
        # EVERY route is failing — which is the condition it should describe.
        breaker=CircuitBreaker(threshold=settings.llm_circuit_breaker_failures),
    )
