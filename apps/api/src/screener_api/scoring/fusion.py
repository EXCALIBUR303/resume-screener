"""Weighted fusion, with every term exposed.

"Why is A above B?" must be answerable as a diff of two component vectors, not
as a model's opinion. Every number below is either computed in Python or capped
at a configured weight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from screener_api.scoring.deterministic import DeterministicScore
from screener_api.scoring.evidence import VerificationResult

# The rubric term is the ONLY model-influenced input, and it is capped. Even a
# model that has been completely fooled cannot move more than this share.
DEFAULT_WEIGHTS: dict[str, float] = {
    "skill": 0.30,
    "experience": 0.20,
    "semantic": 0.20,
    "rubric": 0.30,
}

PENALTIES: dict[str, float] = {
    "low_ocr_confidence": 0.10,
    "partially_supported": 0.15,
    "injection_suspected": 0.20,
    # Named-but-never-demonstrated skills. Distinct from injection: stuffing
    # carries no instruction language, so the detector never fires and this is
    # the only thing standing between it and a free score (ADR-0014).
    "keyword_stuffing": 0.15,
    "degraded": 0.10,
}


@dataclass
class Contribution:
    term: str
    weight: float
    value: float
    computed_by: str  # "python" or "model"

    @property
    def points(self) -> float:
        return round(self.weight * self.value, 4)


@dataclass
class FusedScore:
    total: float
    contributions: list[Contribution] = field(default_factory=list)
    penalties_applied: dict[str, float] = field(default_factory=dict)
    degraded: bool = False
    partially_supported: bool = False
    injection_suspected: bool = False
    keyword_stuffing: bool = False
    hard_gate_failures: list[str] = field(default_factory=list)

    @property
    def out_of_ten(self) -> float:
        return round(self.total * 10, 2)

    @property
    def model_influence(self) -> float:
        """How much of the score the model could possibly have moved."""
        return sum(c.weight for c in self.contributions if c.computed_by == "model")

    def explain(self) -> list[str]:
        lines = [
            f"{c.term:<12} weight {c.weight:.2f} x value {c.value:.2f} "
            f"= {c.points:+.3f}  [{c.computed_by}]"
            for c in self.contributions
        ]
        lines += [
            f"{'penalty':<12} {name:<24} = {-value:+.3f}"
            for name, value in self.penalties_applied.items()
        ]
        lines.append(f"{'TOTAL':<12} {self.total:.3f}  ({self.out_of_ten}/10)")
        return lines


def fuse_score(
    *,
    deterministic: DeterministicScore,
    verification: VerificationResult | None,
    semantic_score: float,
    weights: dict[str, float] | None = None,
    degraded: bool = False,
    injection_suspected: bool = False,
    low_ocr_confidence: bool = False,
) -> FusedScore:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    contributions = [
        Contribution("skill", w["skill"], deterministic.skill_score, "python"),
        Contribution("experience", w["experience"], deterministic.experience_score, "python"),
        Contribution("semantic", w["semantic"], round(semantic_score, 4), "python"),
    ]

    if verification is None:
        # Degraded: the model was unavailable. The remaining terms are
        # renormalised so a missing rubric does not silently look like a zero
        # rubric — a candidate must not be punished for our outage.
        total_weight = sum(c.weight for c in contributions)
        raw = sum(c.points for c in contributions) / total_weight if total_weight else 0.0
        rubric_value = 0.0
        degraded = True
    else:
        rubric_value = verification.mean_effective_level / 4.0
        contributions.append(Contribution("rubric", w["rubric"], round(rubric_value, 4), "model"))
        raw = sum(c.points for c in contributions)

    penalties: dict[str, float] = {}
    partially = bool(verification and verification.partially_supported)
    if partially:
        penalties["partially_supported"] = PENALTIES["partially_supported"]
    if injection_suspected:
        penalties["injection_suspected"] = PENALTIES["injection_suspected"]
    if low_ocr_confidence:
        penalties["low_ocr_confidence"] = PENALTIES["low_ocr_confidence"]
    if deterministic.looks_stuffed:
        penalties["keyword_stuffing"] = PENALTIES["keyword_stuffing"]
    if degraded:
        penalties["degraded"] = PENALTIES["degraded"]

    total = max(0.0, min(1.0, raw) - sum(penalties.values()))

    return FusedScore(
        total=round(max(0.0, total), 4),
        contributions=contributions,
        penalties_applied=penalties,
        degraded=degraded,
        partially_supported=partially,
        injection_suspected=injection_suspected,
        keyword_stuffing=deterministic.looks_stuffed,
        hard_gate_failures=list(deterministic.hard_gate_failures),
    )
