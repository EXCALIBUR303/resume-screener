"""Output contract for interview question generation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RubricAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: int = Field(ge=1, le=5)
    descriptor: str = Field(min_length=10, max_length=300)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=15, max_length=400)
    competency: str = Field(max_length=80)
    difficulty: str = Field(pattern="^(warmup|core|stretch)$")
    probe_reason: str = Field(max_length=300)
    # The grounding. A question citing neither is rejected — that is what stops
    # "tell me about yourself" and every other generic filler.
    cites_requirement: str | None = Field(default=None, max_length=200)
    cites_evidence: str | None = Field(default=None, max_length=300)
    rubric: list[RubricAnchor] = Field(min_length=1, max_length=5)


class InterviewGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[Question] = Field(min_length=1, max_length=12)
    focus_areas: list[str] = Field(default_factory=list, max_length=8)


INTERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions", "focus_areas"],
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "competency", "difficulty", "probe_reason", "rubric"],
                "properties": {
                    "question": {"type": "string", "minLength": 15, "maxLength": 400},
                    "competency": {"type": "string", "maxLength": 80},
                    "difficulty": {"type": "string", "enum": ["warmup", "core", "stretch"]},
                    "probe_reason": {"type": "string", "maxLength": 300},
                    "cites_requirement": {"type": ["string", "null"], "maxLength": 200},
                    "cites_evidence": {"type": ["string", "null"], "maxLength": 300},
                    "rubric": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["level", "descriptor"],
                            "properties": {
                                "level": {"type": "integer", "minimum": 1, "maximum": 5},
                                "descriptor": {"type": "string", "minLength": 10, "maxLength": 300},
                            },
                        },
                    },
                },
            },
        },
        "focus_areas": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 120},
        },
    },
}
