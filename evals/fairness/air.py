"""Adverse impact ratio, and an honest account of what it means here.

The four-fifths rule is the EEOC's rule of thumb for *applicant flow data*: the
selection rate of the least-selected group divided by that of the most-selected
group, with a ratio under 0.80 treated as evidence worth investigating.

Computing it over synthetic counterfactuals is **not** that measurement, and
this module is not a compliance artifact. Three differences matter:

1. There is no applicant flow. Every "group" here is the same invented resume
   with one token changed, so a group is a construction, not a population.
2. The scorer is deterministic. Where the counterfactual-invariance check
   passes, the AIR is 1.0 by arithmetic necessity — it adds no information the
   invariance result did not already carry.
3. n is tiny. With a handful of resumes per group a single flip moves the ratio
   by a large step, so the number has no confidence interval worth quoting.

It is computed anyway for one reason: it is the metric a reader will look for,
and reporting it alongside its own limitations is better than omitting it and
leaving the impression it was measured and hidden. Where it is informative is
the failing case — an AIR below 1.0 here means the pipeline responded to a
token it was supposed to have removed, and that is a real defect regardless of
how small the sample is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

FOUR_FIFTHS = 0.80


@dataclass(frozen=True)
class GroupRate:
    group: str
    selected: int
    total: int

    @property
    def rate(self) -> float:
        return self.selected / self.total if self.total else 0.0


@dataclass(frozen=True)
class AdverseImpact:
    axis: str
    threshold: float
    groups: tuple[GroupRate, ...]
    ratio: float
    # Present only when at least one group differs from another; a uniform
    # result carries no signal and saying "0.80 satisfied" about it would be
    # dressing up an identity as a finding.
    informative: bool

    @property
    def meets_four_fifths(self) -> bool:
        return self.ratio >= FOUR_FIFTHS


def selection_rates(scores_by_group: Mapping[str, Sequence[float]],
                    *, threshold: float) -> tuple[GroupRate, ...]:
    return tuple(
        GroupRate(group=group, selected=sum(1 for s in scores if s >= threshold),
                  total=len(scores))
        for group, scores in sorted(scores_by_group.items())
    )


def adverse_impact_ratio(axis: str, scores_by_group: Mapping[str, Sequence[float]],
                         *, threshold: float) -> AdverseImpact:
    groups = selection_rates(scores_by_group, threshold=threshold)
    rates = [g.rate for g in groups if g.total]
    if not rates:
        return AdverseImpact(axis, threshold, groups, 0.0, informative=False)

    highest = max(rates)
    # Every group rejected is not parity, it is a threshold set above the whole
    # sample. Reporting 1.0 there would claim a fairness result from a run that
    # selected nobody.
    ratio = (min(rates) / highest) if highest > 0 else 0.0
    informative = highest > 0 and len(set(rates)) > 1
    return AdverseImpact(axis, threshold, groups, round(ratio, 4), informative=informative)
