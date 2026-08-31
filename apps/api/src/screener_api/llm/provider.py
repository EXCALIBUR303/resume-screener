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
            text=self._synthesise(user, schema),
            model_id=self.model_id,
            prompt_tokens=len(user) // 4,
            completion_tokens=64,
            latency_ms=0.0,
        )

    def _synthesise(self, user: str, schema: dict[str, Any] | None) -> str:
        """Build a valid response for WHATEVER schema was requested.

        The first version hardcoded one shape and could therefore only exercise
        one code path — the interview guide failed schema validation the moment
        it was added. Deriving the response from the schema means every
        structured call the project ever adds is testable offline, for free.

        Quotes are drawn from real lines of the prompt, so evidence verification
        is genuinely exercised rather than bypassed: a stub that invented quotes
        would pass in tests and fail in production.
        """
        seed = int(hashlib.sha256(user.encode()).hexdigest()[:8], 16)
        quotes = [
            ln.strip()
            for ln in user.split("\n")
            if 20 <= len(ln.strip()) <= 200 and not ln.strip().startswith("<")
        ] or ["no usable evidence in the document"]
        return json.dumps(self._value_for(schema or {}, seed, quotes))

    def _value_for(self, schema: dict[str, Any], seed: int, quotes: list[str]) -> Any:
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")

        if kind == "object":
            required = set(schema.get("required", []))
            properties: dict[str, Any] = schema.get("properties", {})
            return {
                name: self._value_for(sub, seed + i, quotes)
                for i, (name, sub) in enumerate(properties.items())
                if name in required or seed % 2 == 0
            }
        if kind == "array":
            item_schema = schema.get("items", {"type": "string"})
            count = max(int(schema.get("minItems", 1)), 1)
            count = min(count + 1, int(schema.get("maxItems", count + 1)))
            return [self._value_for(item_schema, seed + i, quotes) for i in range(count)]
        if kind == "integer":
            low = int(schema.get("minimum", 0))
            high = int(schema.get("maximum", low + 4))
            return low + (seed % max(1, high - low + 1))
        if kind == "number":
            return float(seed % 100) / 100
        if kind == "boolean":
            return bool(seed % 2)

        # Strings: satisfy enum, then length bounds, and prefer a real quote so
        # evidence verification has something genuine to check.
        if "enum" in schema:
            options = list(schema["enum"])
            return options[seed % len(options)]
        minimum = int(schema.get("minLength", 0))
        maximum = int(schema.get("maxLength", 300))
        value = quotes[seed % len(quotes)]
        if len(value) < minimum:
            value = (value + " deterministic stub output")[: max(minimum, len(value))]
            value = value.ljust(minimum, ".")
        return value[:maximum]
