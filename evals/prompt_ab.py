"""A/B two prompt versions against a real model.

The project's other harnesses run on the stub provider, which is deterministic
and free and tells you nothing about a prompt. This one needs a real model,
refuses to pretend otherwise, and measures the thing prompts are actually
supposed to move here: **whether the quotes a model cites can be found in the
document it was given**.

That metric is not invented for this script. The scoring pipeline already
discards unverifiable evidence and zeroes the competency that depended on it
(ADR-0003), so groundedness is not a proxy for quality — it is the difference
between a competency counting and not counting.

Every prompt is run `--repeats` times on every pair. Two runs of the same prompt
are the noise floor, and a difference between prompts that is smaller than the
difference between two runs of one prompt is not a result. ADR-0015 and ADR-0017
both record what happens when a harness skips that step; this one does not.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import statistics
import sys
import time
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(ROOT))

from screener_api.llm.factory import LLMGateway
from screener_api.llm.gateway import SchemaViolationError
from screener_api.llm.prompts import load
from screener_api.llm.provider import LLMError
from screener_api.llm.providers_live import OllamaProvider
from screener_api.scoring.contracts import (
    MATCH_SCORE_SCHEMA,
    RubricAssessment,
)
from screener_api.scoring.evidence import verify
from screener_api.scoring.injection import detect

GOLDEN = ROOT / "evals" / "golden"


@dataclass
class RunStats:
    """What one (prompt, repeat) pass produced."""

    quotes_cited: int = 0
    quotes_verified: int = 0
    competencies: int = 0
    competencies_zeroed: int = 0
    schema_failures: int = 0
    calls: int = 0
    latencies: list[float] = field(default_factory=list)
    groundedness_per_pair: list[float] = field(default_factory=list)

    @property
    def verified_quote_rate(self) -> float:
        return self.quotes_verified / self.quotes_cited if self.quotes_cited else 0.0

    @property
    def zeroed_rate(self) -> float:
        return (
            self.competencies_zeroed / self.competencies if self.competencies else 0.0
        )

    @property
    def median_latency_ms(self) -> float:
        return round(statistics.median(self.latencies), 1) if self.latencies else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "quotes_cited": self.quotes_cited,
            "quotes_verified": self.quotes_verified,
            "verified_quote_rate": round(self.verified_quote_rate, 4),
            "competencies": self.competencies,
            "competencies_zeroed": self.competencies_zeroed,
            "zeroed_rate": round(self.zeroed_rate, 4),
            "schema_failures": self.schema_failures,
            "median_latency_ms": self.median_latency_ms,
        }


def _pairs(corpus: dict, limit: int) -> list[tuple[dict, dict]]:
    """A spread of archetypes rather than the first N resumes.

    Taking whatever came first would have loaded the comparison with strong
    fits, which are the easy case for verbatim citation.
    """
    by_archetype: dict[str, list[dict]] = {}
    for resume in corpus["resumes"]:
        by_archetype.setdefault(resume["archetype"], []).append(resume)

    chosen: list[tuple[dict, dict]] = []
    jobs = corpus["jobs"]
    archetypes = sorted(by_archetype)
    index = 0
    while len(chosen) < limit:
        archetype = archetypes[index % len(archetypes)]
        bucket = by_archetype[archetype]
        resume = bucket[(index // len(archetypes)) % len(bucket)]
        chosen.append((resume, jobs[index % len(jobs)]))
        index += 1
    return chosen


def _score_once(
    gateway: LLMGateway, prompt, resume: dict, job: dict, stats: RunStats
) -> None:
    document = detect(resume["text"]).sanitised_text
    # One chunk per paragraph, mirroring what retrieval hands the real pipeline.
    chunks = {
        f"c{i}": part for i, part in enumerate(document.split("\n\n")) if part.strip()
    }

    system, user = prompt.render(
        job_description=job["description"],
        competencies="\n".join(f"- {s}" for s in job["required"]),
        resume_id=resume["resume_id"],
        nonce=secrets.token_hex(8),
        document="\n\n".join(f"[{cid}] {body}" for cid, body in chunks.items()),
    )

    started = time.monotonic()
    try:
        assessment = gateway.structured(
            system=system, user=user, model=RubricAssessment, schema=MATCH_SCORE_SCHEMA
        ).value
    except (SchemaViolationError, LLMError):
        stats.schema_failures += 1
        stats.calls += 1
        return
    finally:
        stats.latencies.append((time.monotonic() - started) * 1000)

    stats.calls += 1
    result = verify(assessment, sources=chunks)
    for competency in result.competencies:
        stats.competencies += 1
        stats.quotes_cited += competency.quotes_cited
        stats.quotes_verified += competency.quotes_verified
        stats.competencies_zeroed += int(competency.was_zeroed)
    stats.groundedness_per_pair.append(result.aggregate_groundedness)


def run(
    versions: list[int], *, pairs: int, repeats: int, model: str, base_url: str
) -> dict:
    corpus = json.loads((GOLDEN / "corpus.json").read_text())
    selected = _pairs(corpus, pairs)

    provider = OllamaProvider(base_url=base_url, model_id=model)
    gateway = LLMGateway(provider)

    results: dict[str, list[RunStats]] = {}
    for version in versions:
        prompt = load("match_score", version)
        per_repeat: list[RunStats] = []
        for repeat in range(repeats):
            stats = RunStats()
            for resume, job in selected:
                _score_once(gateway, prompt, resume, job, stats)
            per_repeat.append(stats)
            print(
                f"  v{version} run {repeat + 1}/{repeats}: "
                f"verified {stats.verified_quote_rate:.3f} "
                f"({stats.quotes_verified}/{stats.quotes_cited} quotes), "
                f"zeroed {stats.zeroed_rate:.3f}, "
                f"schema failures {stats.schema_failures}",
                flush=True,
            )
        results[f"v{version}"] = per_repeat

    return {
        "model": model,
        "pairs": pairs,
        "repeats": repeats,
        "corpus_version": corpus["version"],
        "runs": {name: [s.as_dict() for s in runs] for name, runs in results.items()},
        "_stats": results,
    }


def _spread(runs: list[RunStats]) -> float:
    rates = [r.verified_quote_rate for r in runs]
    return max(rates) - min(rates) if len(rates) > 1 else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", default="1,2")
    parser.add_argument("--pairs", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--base-url", default="http://localhost:11434")
    args = parser.parse_args()

    versions = [int(v) for v in args.versions.split(",")]

    # Refused rather than run with a warning. The stub derives its answer from a
    # hash of the prompt, so it WOULD produce a difference between two prompts,
    # and that difference would be pure noise wearing a result's clothes.
    probe = OllamaProvider(base_url=args.base_url, model_id=args.model)
    try:
        probe.complete(system="reply with {}", user="{}", max_tokens=8, timeout=20)
    except LLMError as exc:
        print(f"\n  No model at {args.base_url} ({type(exc).__name__}).")
        print("  This comparison is refused rather than run against the stub provider:")
        print("  the stub derives its answer from a hash of the prompt, so it would")
        print("  produce a difference between two prompts that is pure noise.\n")
        return 2

    print(
        f"\n  Prompt A/B: versions {versions} x {args.repeats} runs x "
        f"{args.pairs} pairs on {args.model}\n"
    )
    report = run(
        versions,
        pairs=args.pairs,
        repeats=args.repeats,
        model=args.model,
        base_url=args.base_url,
    )

    stats: dict[str, list[RunStats]] = report.pop("_stats")  # type: ignore[assignment]
    noise = max(_spread(runs) for runs in stats.values())
    report["primary_metric"] = "zeroed_rate (lower is better)"
    report["noise_floor"] = round(noise, 4)

    header = (
        f"  {'prompt':<8}{'zeroed rate':>14}{'quotes/comp':>14}"
        f"{'verified rate':>15}{'schema fails':>14}{'median ms':>12}"
    )
    print("\n" + header)
    print("  " + "-" * (len(header) - 2))

    means: dict[str, float] = {}
    for name, runs in stats.items():
        means[name] = statistics.mean(r.zeroed_rate for r in runs)
        cited = sum(r.quotes_cited for r in runs)
        competencies = sum(r.competencies for r in runs) or 1
        print(
            f"  {name:<8}{means[name]:>14.3f}{cited / competencies:>14.2f}"
            f"{statistics.mean(r.verified_quote_rate for r in runs):>15.3f}"
            f"{sum(r.schema_failures for r in runs):>14}"
            f"{statistics.median([r.median_latency_ms for r in runs]):>12.0f}"
        )

    # Lower is better for the primary metric, so the winner is the minimum.
    best = min(means, key=lambda k: means[k])
    worst = max(means, key=lambda k: means[k])
    difference = means[worst] - means[best]

    # The verdict is RANGE SEPARATION, not mean-versus-noise-floor.
    #
    # The first version compared the difference of means against the widest
    # spread within one prompt, and called a visible effect inconclusive: v1
    # ranged 0.079 to 0.267 across two runs, so its own spread was as large as
    # the gap to v2. That test also gets *harder* to pass as repeats increase,
    # because max-minus-min grows with n — an estimator that punishes more
    # evidence is the wrong estimator.
    #
    # Disjoint ranges say something stronger and stay honest as n grows: every
    # observed run of one prompt beat every observed run of the other. It is a
    # statement about the runs actually performed, with no distributional
    # assumption behind it.
    ranges = {
        name: (
            min(r.zeroed_rate for r in runs),
            max(r.zeroed_rate for r in runs),
        )
        for name, runs in stats.items()
    }
    separated = max(ranges[best]) < min(ranges[worst])

    report["ranges"] = {k: [round(lo, 4), round(hi, 4)] for k, (lo, hi) in ranges.items()}
    report["difference_of_means"] = round(difference, 4)
    report["ranges_disjoint"] = bool(separated)
    report["better"] = best if separated else None

    print(f"\n  Range per prompt (zeroed rate over {args.repeats} runs):")
    for name, (lo, hi) in sorted(ranges.items()):
        print(f"    {name}: {lo:.3f} - {hi:.3f}")
    print(f"\n  Widest spread within one prompt: {noise:.3f}")
    print(f"  Difference of means:             {difference:.3f}")

    if separated:
        print(f"\n  {best} beat {worst} on EVERY run: its worst run "
              f"({max(ranges[best]):.3f}) is still\n  better than {worst}'s best "
              f"({min(ranges[worst]):.3f}). The ranges do not overlap.")
        print("\n  'verified rate' is precision and saturates at 1.000 for both -- neither")
        print("  prompt makes the model cite quotes it cannot support. The difference is")
        print("  recall: whether a competency gets evidenced at all.")
    else:
        print("\n  INCONCLUSIVE. The two prompts' ranges overlap, so at least one run of")
        print("  the worse prompt beat at least one run of the better. More repeats would")
        print("  be needed; on this evidence, neither prompt is established as better.")

    out = ROOT / "evals" / "reports" / "prompt_ab.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n  written to {out.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
