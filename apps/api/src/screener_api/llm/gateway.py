"""The single door to any model.

Everything a caller needs to get right — budget, breaker, schema validation, the
one repair attempt, prompt versioning — lives here, so a call site cannot forget
any of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from screener_api.llm.provider import (
    BudgetExceededError,
    CircuitBreaker,
    Completion,
    LLMError,
    LLMProvider,
    TokenBudget,
)

log = structlog.get_logger()

ModelT = TypeVar("ModelT", bound=BaseModel)


class SchemaViolationError(LLMError):
    """Output failed validation after the repair attempt. Terminal."""


@dataclass
class GatewayResult[ModelT: BaseModel]:
    value: ModelT
    completion: Completion
    repaired: bool = False


class LLMGateway:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget: TokenBudget | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget or TokenBudget()
        self.breaker = breaker or CircuitBreaker()

    def structured(
        self,
        *,
        system: str,
        user: str,
        model: type[ModelT],
        schema: dict[str, Any],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> GatewayResult[ModelT]:
        """Get schema-valid structured output, or raise.

        Exactly one repair attempt. Retrying a malformed response indefinitely
        burns time and hides a real prompt problem; the failure mode table calls
        for one repair, then terminal.
        """
        if self.breaker.is_open:
            raise LLMError("circuit breaker open; model host is failing")
        self.budget.check()

        completion = self._call(system, user, schema, temperature, max_tokens, timeout)
        try:
            return GatewayResult(value=self._parse(completion.text, model), completion=completion)
        except (ValidationError, json.JSONDecodeError) as first_error:
            log.warning("llm.schema_invalid", error=str(first_error)[:200])

        repair_user = (
            f"{user}\n\n"
            "Your previous response was not valid against the required schema. "
            "Return ONLY a JSON object matching the schema exactly, with no "
            "commentary, no markdown fences, and no additional keys."
        )
        repaired = self._call(system, repair_user, schema, temperature, max_tokens, timeout)
        try:
            return GatewayResult(
                value=self._parse(repaired.text, model), completion=repaired, repaired=True
            )
        except (ValidationError, json.JSONDecodeError) as second_error:
            raise SchemaViolationError(
                f"invalid after repair: {str(second_error)[:200]}"
            ) from second_error

    def _call(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Completion:
        try:
            completion = self.provider.complete(
                system=system,
                user=user,
                schema=schema,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except BudgetExceededError:
            raise
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        self.budget.record(completion.total_tokens)
        return completion

    @staticmethod
    def _parse(raw: str, model: type[ModelT]) -> ModelT:
        text = raw.strip()
        # Models wrap JSON in markdown fences even when told not to.
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            text = text[4:] if text.startswith("json") else text
            text = text.strip()
        return model.model_validate_json(text)
