"""AC-10: fail the build when retrieval quality regresses.

Tolerance is 0.03 absolute on nDCG@10, from the blueprint. A tolerance exists
because the harness is not perfectly deterministic across platforms (ONNX
kernels differ); it is small enough that a real regression cannot hide inside it.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOLERANCE = 0.03


def main() -> int:
    baseline_path = ROOT / "evals" / "baselines" / "v1.json"
    latest_path = ROOT / "evals" / "reports" / "latest.json"

    if not latest_path.exists():
        print("no run to compare; run `make eval` first")
        return 1

    baseline = json.loads(baseline_path.read_text())
    latest = json.loads(latest_path.read_text())

    print(f"{'retriever':<12}{'baseline':>10}{'latest':>10}{'delta':>10}")
    print("-" * 42)

    failures: list[str] = []
    for name, base in baseline["retrievers"].items():
        new = latest["retrievers"].get(name)
        if new is None:
            failures.append(f"{name}: missing from this run")
            continue
        delta = new["ndcg10"] - base["ndcg10"]
        flag = "" if delta >= -TOLERANCE else "   <-- REGRESSION"
        print(f"{name:<12}{base['ndcg10']:>10.3f}{new['ndcg10']:>10.3f}"
              f"{delta:>+10.3f}{flag}")
        if delta < -TOLERANCE:
            failures.append(
                f"{name}: nDCG@10 fell {abs(delta):.3f} (tolerance {TOLERANCE})"
            )

    if failures:
        print("\nAC-10 FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print("\nIf the change is intentional, run `make eval-baseline` and commit "
              "the new baseline WITH the change that caused it.")
        return 1

    print("\nAC-10 passed: no retriever regressed beyond tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
