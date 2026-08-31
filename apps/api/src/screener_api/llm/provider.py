"""LLM provider abstraction — the decision the whole project rests on.

Three implementations behind one Protocol:

* ``stub``  — deterministic, offline, free. **Every test and every CI run uses
  this.** It is why the suite is fast, reproducible, and costs nothing.
* ``ollama`` — local models, the development default.
* ``openai_compatible`` — any free-tier endpoint, for the cloud demo.

The model is a pure text→JSON transform. It has no tools, no network of its own,
no database access. OWASP LLM06 (excessive agency) is designed out rather than
mitigated, because there is nothing for a compromised prompt to reach.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

log = structlog.get_logger()


class LLMError(Exception):
    """Base for provider failures."""


class LLMTimeoutError(LLMError):
    """The model did not answer inside the deadline. Retryable."""


class LLMUnavailableError(LLMError):
    """The model host could not be reached. Retryable."""


class BudgetExceededError(LLMError):
    """The token budget is spent. Terminal for this request, not the job."""


@dataclass(frozen=True)
class Completion:
    text: str
    model_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class LLMProvider(Protocol):
    """The only surface the rest of the codebase may use to reach a model."""

    model_id: str

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> Completion: ...


# --------------------------------------------------------------------------- #
#  Budget and circuit breaker
# --------------------------------------------------------------------------- #


@dataclass
class TokenBudget:
    """A hard ceiling that ships enabled.

    The blueprint's rule: no architecture that creates hidden ongoing costs.
    With a local model the budget is free but still enforced, so switching to a
    paid endpoint cannot quietly run up a bill — the limit is already there and
    already tested.
    """

    max_tokens: int = 0  # 0 = unlimited (safe default for local models)
    spent: int = 0

    def check(self) -> None:
        if self.max_tokens and self.spent >= self.max_tokens:
            raise BudgetExceededError(f"token budget exhausted: {self.spent}/{self.max_tokens}")

    def record(self, tokens: int) -> None:
        self.spent += tokens


@dataclass
class CircuitBreaker:
    """Stop hammering a model host that is clearly down."""

    threshold: int = 5
    reset_after_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at > self.reset_after_seconds:
            self.failures = 0
            self.opened_at = None
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            log.warning("llm.circuit_opened", failures=self.failures)


# --------------------------------------------------------------------------- #
#  Stub — the keystone
# --------------------------------------------------------------------------- #


@dataclass
class StubProvider:
    """Deterministic responses derived from the prompt hash.

    Not a mock that returns one canned string: it produces *schema-valid,
    evidence-citing* output whose content is a pure function of the input, so
    tests can assert on real behaviour. Where a test needs a specific response,
    it registers one by prompt substring.
    """

    model_id: str = "stub-v1"
    responses: dict[str, str] = field(default_factory=dict)
    # Applied on the first matching call only. The repair prompt contains the
    # original user text, so a plain `responses` entry matches BOTH calls and
    # the repair path can never be exercised.
    once: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Exception | None = None
    _malformed_once: bool = False

    def register(self, match: str, response: str) -> None:
        self.responses[match] = response

    def register_once(self, match: str, response: str) -> None:
        """Respond this way to the first matching call only."""
        self.once[match] = response

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
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "temperature": temperature}
        )
        if self.fail_with is not None:
            raise self.fail_with

        for needle in list(self.once):
            if needle in user or needle in system:
                return Completion(
                    text=self.once.pop(needle),
                    model_id=self.model_id,
                    prompt_tokens=len(user) // 4,
                    completion_tokens=64,
                )

        for needle, response in self.responses.items():
            if needle in user or needle in system:
                return Completion(
                    text=response,
                    model_id=self.model_id,
                    prompt_tokens=len(user) // 4,
                    completion_tokens=64,
                )

        return Completion(
            text=self._synthesise(user),
            model_id=self.model_id,
            prompt_tokens=len(user) // 4,
            completion_tokens=64,
            latency_ms=0.0,
        )

    def _synthesise(self, user: str) -> str:
        """Build a valid response that cites text genuinely present in the input.

        Citing real substrings matters: a stub that invented quotes would make
        the evidence verifier pass in tests and fail in production.
        """
        seed = int(hashlib.sha256(user.encode()).hexdigest()[:8], 16)
        lines = [
            ln.strip()
            for ln in user.split("\n")
            if 20 <= len(ln.strip()) <= 200 and not ln.strip().startswith("<")
        ]
        quote = lines[seed % len(lines)] if lines else "no usable evidence"
        return json.dumps(
            {
                "competencies": [
                    {
                        "name": "Python",
                        "level": seed % 5,
                        "evidence": [{"chunk_id": "c0", "quote": quote}],
                    },
                    {
                        "name": "Databases",
                        "level": (seed // 5) % 5,
                        "evidence": [{"chunk_id": "c0", "quote": quote}],
                    },
                ],
                "unmet_requirements": ["Kubernetes"] if seed % 2 else [],
                "overall_rationale": "Deterministic stub assessment.",
            }
        )
