"""Ollama and OpenAI-compatible providers."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import structlog

from screener_api.llm.provider import (
    Completion,
    LLMTimeoutError,
    LLMUnavailableError,
)

log = structlog.get_logger()


ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeEndpointError(LLMUnavailableError):
    """The configured model endpoint is not an http(s) URL."""


def _post(
    url: str, payload: dict[str, Any], *, timeout: float, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    # urlopen honours file:// and other schemes. LLM_BASE_URL comes from
    # configuration, so a mistake there would turn a model call into a local
    # file read. Validate the scheme rather than suppress the warning.
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeEndpointError(
            f"refusing to call a {scheme or 'schemeless'} endpoint; "
            f"only {sorted(ALLOWED_SCHEMES)} are permitted"
        )

    request = urllib.request.Request(  # noqa: S310  # nosec B310
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        # Scheme validated above.
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310  # noqa: S310
            return dict(json.loads(response.read()))
    except TimeoutError as exc:
        raise LLMTimeoutError(f"no response within {timeout}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LLMTimeoutError(f"no response within {timeout}s") from exc
        raise LLMUnavailableError(str(exc.reason)) from exc


@dataclass
class OllamaProvider:
    """Local models. The development default: free, private, offline."""

    base_url: str = "http://host.docker.internal:11434"
    model_id: str = "qwen3:8b"

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
        payload: dict[str, Any] = {
            "model": self.model_id,
            "system": system,
            "prompt": user,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens, "top_p": 0.9},
        }
        # A JSON Schema here is constrained decoding, not a suggestion: the
        # runtime cannot emit tokens that violate it. Measured at 4/4 valid in
        # the ADR-0003 spike.
        if schema is not None:
            payload["format"] = schema

        started = time.monotonic()
        body = _post(f"{self.base_url}/api/generate", payload, timeout=timeout)
        return Completion(
            text=str(body.get("response", "")),
            model_id=self.model_id,
            prompt_tokens=int(body.get("prompt_eval_count", 0)),
            completion_tokens=int(body.get("eval_count", 0)),
            latency_ms=(time.monotonic() - started) * 1000,
        )


@dataclass
class OpenAICompatibleProvider:
    """Any OpenAI-shaped endpoint, for the free-tier cloud demo.

    Free tiers commonly reserve the right to train on submitted data. Only
    redacted, synthetic content ever reaches here — which the M4 pipeline
    guarantees upstream, not this class.
    """

    base_url: str
    model_id: str
    api_key: str = ""

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
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            }

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        started = time.monotonic()
        body = _post(f"{self.base_url}/chat/completions", payload, timeout=timeout, headers=headers)
        choices = body.get("choices") or [{}]
        usage = body.get("usage") or {}
        return Completion(
            text=str(choices[0].get("message", {}).get("content", "")),
            model_id=self.model_id,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=(time.monotonic() - started) * 1000,
        )
