# ADR-0019 — Model fallback, and the provenance bug it created

**Status:** accepted · **Date:** 2026-09-01 · **Extends** ADR-0003

## The router

One `LLMProvider` in front of several, tried in order. A router *is* a provider,
so the gateway, the token budget, the schema repair and every call site are
unchanged.

Three decisions, each of which is a way to be subtly wrong rather than
obviously broken.

**Only retryable failures fall through.** A timeout or an unreachable host is
worth another provider's attempt. `BudgetExceededError` is not, and falling
through on it would be worse than useless: the budget is a property of the
*request*, so a second provider would be asked to spend tokens after the
ceiling was reached. The cost control would become an amplifier for it, against
the blueprint's rule that no architecture may create hidden ongoing costs.

**Each route gets its own breaker.** A shared breaker trips on the primary's
failures and then refuses the fallback — unreachable at exactly the moment it
exists to be reached. The test asserts the primary is called twice with a
threshold of two, and the fallback four times across four calls: the dead route
is skipped, not retried.

**The fallback gets the identical prompt.** Not a paraphrase and not the repair
prompt. A fallback that saw different input would produce a result that cannot
be compared with the primary's.

## The bug it created, which was already there

The scoring pipeline recorded provenance like this:

```python
record = Match(..., model_id=gateway.provider.model_id)
```

With one provider that is correct. With a router it is **the model that would
have been tried first**, which is not the model that answered. A fallback could
write the assessment while the stored row named the primary.

That is a worse failure than having no provenance at all, because the row looks
trustworthy. It is also invisible: nothing crashes, the score is plausible, and
the only symptom is that re-running the same job against the same "model" gives
a different answer.

Fixed by reading `result.completion.model_id` — the completion is the only thing
that knows who replied. The configured label survives only on the degraded path,
where nothing replied at all. Four sites needed it, including the `Match`
uniqueness lookup and the outbox event key, so a fallback-produced score is a
distinct row rather than an overwrite of the primary's.

`RoutedProvider.model_id` carries a docstring saying it is a label and not
provenance, because the next person to reach for it will be reaching for the
wrong thing.

## A protocol that was wrong about itself

`LLMProvider` declared `model_id: str` — a *settable* attribute. The router
computes it from its route list, so mypy rejected the router as a provider.

The protocol was the thing that was wrong. Nothing in the codebase assigns to a
provider's `model_id`, and nothing should. It is a read-only property now, which
the concrete dataclass providers still satisfy.

## Why not Langfuse

The blueprint listed "Langfuse prompt A/B" alongside this. Self-hosting Langfuse
means ClickHouse, Redis, MinIO and a web tier — four services and their upkeep,
against a constraint that says to avoid architecture with hidden ongoing costs.

What it would buy is prompt comparison. This project already versions prompts
immutably, records `prompt_version` and `prompt_hash` on every `Match`, and has
an eval harness with a committed baseline. The missing piece is comparing two
prompt versions on the golden set, which is a script rather than a platform.
Declining a listed tool with the reason is a decision; adopting it because it
was on a list is not.

## Consequences

- Off by default. `LLM_FALLBACK_PROVIDER=""` means no router is built at all —
  a fallback nobody configured should not quietly exist.
- A fallback answering is logged at `warning`, not `info`. It may be a weaker
  model, and a score it produced is not comparable to the primary's; the log
  line is how an operator notices it is happening.
- `AllRoutesFailedError` names every route it tried. "The model failed" is not
  actionable.
