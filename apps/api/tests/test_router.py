"""Model router: ordered fallback, and the three ways it could be subtly wrong."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from screener_api.llm.provider import (
    BudgetExceededError,
    CircuitBreaker,
    Completion,
    LLMProvider,
    LLMTimeoutError,
    LLMUnavailableError,
    StubProvider,
)
from screener_api.llm.router import AllRoutesFailedError, Route, RoutedProvider


@dataclass
class FakeProvider:
    """A provider that answers, or fails in a specified way."""

    model_id: str
    fail_with: Exception | None = None
    calls: int = 0
    seen: list[str] = field(default_factory=list)

    def complete(self, *, system: str, user: str, **kw: Any) -> Completion:
        self.calls += 1
        self.seen.append(user)
        if self.fail_with is not None:
            raise self.fail_with
        return Completion(text="{}", model_id=self.model_id, completion_tokens=7)


def _router(*providers: FakeProvider, threshold: int = 5) -> RoutedProvider:
    return RoutedProvider(routes=[Route(p, CircuitBreaker(threshold=threshold)) for p in providers])


def _call(router: RoutedProvider) -> Completion:
    return router.complete(system="s", user="u")


def test_the_primary_answers_and_the_fallback_is_untouched() -> None:
    primary, fallback = FakeProvider("primary"), FakeProvider("fallback")
    assert _call(_router(primary, fallback)).model_id == "primary"
    assert fallback.calls == 0


@pytest.mark.parametrize("failure", [LLMTimeoutError("slow"), LLMUnavailableError("down")])
def test_a_retryable_failure_falls_through(failure: Exception) -> None:
    primary = FakeProvider("primary", fail_with=failure)
    fallback = FakeProvider("fallback")
    assert _call(_router(primary, fallback)).model_id == "fallback"
    assert fallback.calls == 1


def test_an_exhausted_budget_does_not_fall_through() -> None:
    """The ceiling belongs to the request, not to one host.

    Falling through would spend a second provider's tokens after the limit was
    reached, which turns the cost control into an amplifier for it — and the
    blueprint's rule is no architecture that creates hidden ongoing costs.
    """
    primary = FakeProvider("primary", fail_with=BudgetExceededError("spent"))
    fallback = FakeProvider("fallback")
    with pytest.raises(BudgetExceededError):
        _call(_router(primary, fallback))
    assert fallback.calls == 0


def test_every_route_failing_raises_rather_than_returning_nothing() -> None:
    primary = FakeProvider("primary", fail_with=LLMTimeoutError("slow"))
    fallback = FakeProvider("fallback", fail_with=LLMUnavailableError("down"))
    with pytest.raises(AllRoutesFailedError) as caught:
        _call(_router(primary, fallback))
    # The error names what was tried; "the model failed" is not actionable.
    assert "primary" in str(caught.value) and "fallback" in str(caught.value)


def test_each_route_has_its_own_breaker() -> None:
    """A shared breaker trips on the primary's failures and then refuses the
    fallback, so the fallback is unreachable exactly when it is needed."""
    primary = FakeProvider("primary", fail_with=LLMUnavailableError("down"))
    fallback = FakeProvider("fallback")
    router = _router(primary, fallback, threshold=2)

    for _ in range(4):
        assert _call(router).model_id == "fallback"

    assert router.routes[0].breaker.is_open
    assert not router.routes[1].breaker.is_open
    # Once open, the dead primary is skipped instead of retried every time.
    assert primary.calls == 2
    assert fallback.calls == 4


def test_the_completion_names_the_model_that_actually_answered() -> None:
    """`router.model_id` is a label for the route tried FIRST. Persisting it as
    provenance would name the primary while a fallback wrote the answer — the
    bug this router created in the scoring pipeline (ADR-0019)."""
    primary = FakeProvider("primary", fail_with=LLMTimeoutError("slow"))
    router = _router(primary, FakeProvider("fallback"))
    assert router.model_id == "primary"
    assert _call(router).model_id == "fallback"


def test_a_router_is_a_provider() -> None:
    """It has to satisfy the protocol, or the gateway and the budget would need
    to know a router exists."""
    assert isinstance(_router(FakeProvider("a")), LLMProvider)
    assert isinstance(StubProvider(), LLMProvider)


def test_a_router_with_no_routes_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one route"):
        RoutedProvider(routes=[])


def test_a_single_route_router_behaves_like_the_bare_provider() -> None:
    only = FakeProvider("only")
    router = _router(only)
    assert _call(router).model_id == "only"
    assert router.model_id == "only"


def test_the_fallback_receives_the_same_prompt() -> None:
    """Not a paraphrase, not a repair prompt: the identical request. A fallback
    that saw different input would produce a result that is not comparable."""
    primary = FakeProvider("primary", fail_with=LLMTimeoutError("slow"))
    fallback = FakeProvider("fallback")
    _router(primary, fallback).complete(system="sys", user="the exact prompt")
    assert fallback.seen == ["the exact prompt"]


def test_the_factory_builds_a_plain_provider_when_no_fallback_is_configured() -> None:
    """A fallback nobody configured should not quietly exist."""
    from screener_api.llm.factory import build_provider
    from screener_api.settings import Settings

    settings = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        llm_provider="stub",
    )
    assert not isinstance(build_provider(settings), RoutedProvider)


def test_the_factory_builds_a_router_when_a_fallback_is_configured() -> None:
    from screener_api.llm.factory import build_provider
    from screener_api.settings import Settings

    settings = Settings(
        app_env="dev",
        postgres_password="x",
        app_kek="x",
        jwt_secret="x",
        llm_provider="stub",
        llm_fallback_provider="stub",
    )
    provider = build_provider(settings)
    assert isinstance(provider, RoutedProvider)
    assert len(provider.routes) == 2
    # Distinct breaker objects, not one shared instance.
    assert provider.routes[0].breaker is not provider.routes[1].breaker


# --------------------------------------------------------------------------- #
#  Which prompt is actually running
# --------------------------------------------------------------------------- #


def test_the_active_prompt_version_defaults_to_the_latest_on_disk() -> None:
    from screener_api.llm.prompts import active_version, latest_version

    assert active_version("match_score", None) == latest_version("match_score")


def test_an_explicit_pin_wins_over_whatever_is_on_disk() -> None:
    """Prompt files are immutable once committed, but `latest` is implicit — so
    ADDING a file is a deploy. Writing v2 to run an A/B changed what the worker
    scores with, without a code change and without review. The experiment won
    (ADR-0021), so the promotion was correct; it should not have been an
    accident."""
    from screener_api.llm.prompts import active_version, latest_version

    assert latest_version("match_score") >= 2
    assert active_version("match_score", 1) == 1


def test_the_pin_is_off_by_default_so_behaviour_is_unchanged() -> None:
    from screener_api.settings import Settings

    settings = Settings(app_env="dev", postgres_password="x", app_kek="x", jwt_secret="x")
    assert settings.llm_prompt_version is None
