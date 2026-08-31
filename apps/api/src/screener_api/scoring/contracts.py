"""Output contract for the rubric scorer.

Note what is **absent**: there is no free-form ``score`` field. The model emits
levels and evidence; the arithmetic happens in Python where it can be audited
and cannot be argued with by an injected instruction.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str = Field(max_length=80)
    quote: str = Field(min_length=10, max_length=300)


class Competency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)
    level: int = Field(ge=0, le=4)
    evidence: list[Evidence] = Field(default_factory=list, max_length=3)


class RubricAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    competencies: list[Competency] = Field(min_length=1, max_length=12)
    unmet_requirements: list[str] = Field(default_factory=list, max_length=20)
    overall_rationale: str = Field(default="", max_length=800)


MATCH_SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["competencies", "unmet_requirements", "overall_rationale"],
    "properties": {
        "competencies": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "level", "evidence"],
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "level": {"type": "integer", "minimum": 0, "maximum": 4},
                    "evidence": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["chunk_id", "quote"],
                            "properties": {
                                "chunk_id": {"type": "string", "maxLength": 80},
                                "quote": {"type": "string", "minLength": 10, "maxLength": 300},
                            },
                        },
                    },
                },
            },
        },
        "unmet_requirements": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 200},
        },
        "overall_rationale": {"type": "string", "maxLength": 800},
    },
}
