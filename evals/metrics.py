"""Ranking metrics.

Implemented rather than imported so their behaviour is inspectable and unit
tested against hand-computed values — a metric you cannot check by hand is a
number you cannot defend.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def dcg(gains: Sequence[float], k: int) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains[:k]))


def ndcg_at_k(ranked_grades: Sequence[int], k: int = 10) -> float:
    """Normalised discounted cumulative gain.

    Uses the standard 2^g - 1 gain, so grade 3 counts substantially more than
    grade 1 — a screener that surfaces "vaguely related" ahead of "strong fit"
    should be punished more than linearly.
    """
    gains = [2**g - 1 for g in ranked_grades]
    ideal = sorted(gains, reverse=True)
    denominator = dcg(ideal, k)
    return dcg(gains, k) / denominator if denominator else 0.0


def precision_at_k(ranked_grades: Sequence[int], k: int = 5, *, threshold: int = 2) -> float:
    """Fraction of the top k that are genuinely relevant (grade >= threshold)."""
    top = ranked_grades[:k]
    return sum(1 for g in top if g >= threshold) / len(top) if top else 0.0


def recall_at_k(ranked_grades: Sequence[int], all_grades: Sequence[int], k: int = 20,
                *, threshold: int = 2) -> float:
    relevant = sum(1 for g in all_grades if g >= threshold)
    if not relevant:
        return 0.0
    return sum(1 for g in ranked_grades[:k] if g >= threshold) / relevant


def mean_reciprocal_rank(ranked_grades: Sequence[int], *, threshold: int = 2) -> float:
    for i, grade in enumerate(ranked_grades, start=1):
        if grade >= threshold:
            return 1.0 / i
    return 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
