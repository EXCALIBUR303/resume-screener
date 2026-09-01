"""Ordered fallback across model providers.

One `LLMProvider` in front of several, tried in order. The primary is a local
model; a fallback is whatever the operator configured, which in the free-tier
setup is usually a hosted endpoint.

Three things this had to get right, and each is a way to be subtly wrong rather
than obviously broken.

**Only retryable failures fall through.** A timeout or an unreachable host is
worth another provider's attempt. A `BudgetExceededError` is not: the budget is
a property of the *request*, not of one host, and falling through would spend a
second provider's tokens after the ceiling was reached — turning the cost
control into an amplifier for it.

**Each provider gets its own breaker.** A shared breaker trips on the primary's
failures and then refuses the fallback, so the fallback becomes unreachable at
exactly the moment it exists to be reached.

**The answer says who answered.** A score is not interpretable without knowing
what produced it, and this is the one component that can silently change the
answer to "what model was this?" in the middle of a request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from screener_api.llm.provider import (
    BudgetExceededError,
    CircuitBreaker,
    Completion,
    LLMError,
    LLMProvider,
)

log = structlog.get_logger()


@dataclass
class Route:
    """One provider and the breaker that belongs to it alone."""

    provider: LLMProvider
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def model_id(self) -> str:
        return self.provider.model_id


class AllRoutesFailedError(LLMError):
    """Every configured provider refused or failed."""


@dataclass
class RoutedProvider:
    """Try each route in order; the first success wins.

    Satisfies `LLMProvider`, so the gateway, the budget and everything above are
    unchanged — a router is a provider, not a new layer.
    """

    routes: list[Route]

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("a router needs at least one route")

    @property
    def model_id(self) -> str:
        """The label for the route that would be tried first.

        This is NOT what to record as provenance. When a fallback answers, the
        model that produced the text is `completion.model_id`, and only the
        completion knows. Callers that persist a model id must read it from the
        completion — the scoring pipeline was reading this attribute, which was
        correct with one provider and wrong the moment a second existed.
        """
        return self.routes[0].model_id

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> Completion:
        failures: list[str] = []

        for index, route in enumerate(self.routes):
            if route.breaker.is_open:
                failures.append(f"{route.model_id}: breaker open")
                continue
            try:
                completion = route.provider.complete(
                    system=system,
                    user=user,
                    schema=schema,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
            except BudgetExceededError:
                # Never falls through. The ceiling belongs to the request.
                raise
            except LLMError as exc:
                route.breaker.record_failure()
                failures.append(f"{route.model_id}: {type(exc).__name__}")
                log.warning(
                    "llm.route_failed",
                    route=index,
                    model=route.model_id,
                    error=type(exc).__name__,
                )
                continue

            route.breaker.record_success()
            if index > 0:
                # Loud on purpose. A fallback model may be weaker, and a score
                # produced by one is not comparable to a score produced by the
                # primary. The Match row records which one answered; this line
                # is how an operator notices it is happening at all.
                log.warning(
                    "llm.fell_back",
                    route=index,
                    model=completion.model_id,
                    primary=self.routes[0].model_id,
                    skipped=failures,
                )
            return completion

        raise AllRoutesFailedError("; ".join(failures))
