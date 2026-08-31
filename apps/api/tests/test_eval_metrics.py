"""Metrics checked against hand-computed values.

A metric you cannot verify by hand is a number you cannot defend, and the whole
point of this harness is producing numbers that can be defended.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from evals.metrics import (
    dcg,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


def test_perfect_ranking_scores_one() -> None:
    assert ndcg_at_k([3, 3, 2, 1, 0], k=5) == pytest.approx(1.0)


def test_reversed_ranking_scores_less_than_perfect() -> None:
    assert ndcg_at_k([0, 1, 2, 3, 3], k=5) < ndcg_at_k([3, 3, 2, 1, 0], k=5)


def test_ndcg_matches_a_hand_computation() -> None:
    """grades [3, 0, 1] -> gains [7, 0, 1]
    DCG  = 7/log2(2) + 0/log2(3) + 1/log2(4) = 7 + 0 + 0.5      = 7.5
    IDCG = 7/log2(2) + 1/log2(3) + 0/log2(4) = 7 + 0.6309...    = 7.6309...
    """
    expected = 7.5 / (7 + 1 / math.log2(3))
    assert ndcg_at_k([3, 0, 1], k=3) == pytest.approx(expected)


def test_ndcg_is_zero_when_nothing_is_relevant() -> None:
    assert ndcg_at_k([0, 0, 0], k=3) == 0.0


def test_gain_is_exponential_not_linear() -> None:
    """One grade-3 hit outweighs three grade-1 hits.

    Asserted on DCG, not nDCG: nDCG normalises against the ideal ordering of the
    SAME grades, so [3,0,0] and [1,1,1] are each perfectly ordered and both
    score 1.0. My first version of this test compared them and was simply
    measuring the wrong thing.
    """
    assert dcg([2**3 - 1, 0, 0], 3) > dcg([2**1 - 1] * 3, 3)


def test_misplacing_a_strong_fit_costs_more_than_misplacing_a_weak_one() -> None:
    """The property that actually matters for a screener: burying a grade-3
    candidate is punished harder than burying a grade-1 one."""
    buried_strong = ndcg_at_k([1, 1, 3], k=3)
    buried_weak = ndcg_at_k([3, 3, 1], k=3)
    assert buried_strong < buried_weak


def test_precision_counts_only_genuine_matches() -> None:
    assert precision_at_k([3, 2, 1, 0, 0], k=5) == pytest.approx(0.4)
    assert precision_at_k([3, 3, 3, 3, 3], k=5) == 1.0
    assert precision_at_k([1, 1, 0], k=3) == 0.0


def test_recall_is_against_all_relevant_documents() -> None:
    every = [3, 2, 2, 0, 0, 0]  # three relevant in the whole corpus
    assert recall_at_k([3, 2], every, k=2) == pytest.approx(2 / 3)
    assert recall_at_k([3, 2, 2], every, k=3) == 1.0


def test_mrr_finds_the_first_relevant_position() -> None:
    assert mean_reciprocal_rank([0, 0, 2]) == pytest.approx(1 / 3)
    assert mean_reciprocal_rank([3]) == 1.0
    assert mean_reciprocal_rank([0, 0, 0]) == 0.0


def test_empty_input_is_safe() -> None:
    assert ndcg_at_k([], k=10) == 0.0
    assert precision_at_k([], k=5) == 0.0
    assert mean_reciprocal_rank([]) == 0.0
    assert recall_at_k([], [], k=10) == 0.0
