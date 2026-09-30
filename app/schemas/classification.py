from typing import Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, validator

INTENTS = [
    "internship_course_inquiry",
    "meeting_request",
    "pricing_request",
    "support_question",
    "follow_up",
    "acknowledgement",
    "spam_marketing",
    "sensitive_high_risk",
    "unknown",
]

RISK_LEVELS = ["low", "medium", "high"]

class ClassificationResult(BaseModel):
    intent: str = Field(..., description=f"One of {INTENTS}")
    confidence: float = Field(..., ge=0.0, le=1.0, description="0-1 confidence")
    needs_reply: bool = Field(..., description="Whether automated reply needed")
    risk_level: str = Field(..., description="low/medium/high")
    reason: str = Field(..., description="Short human-readable reason")
    extracted_fields: Dict[str, Any] = Field(default_factory=dict, description="Structured fields like dates, names, topics")

    @validator("intent")
    def _valid_intent(cls, v):
        if v not in INTENTS:
            raise ValueError(f"intent must be one of {INTENTS}")
        return v

    @validator("risk_level")
    def _valid_risk(cls, v):
        if v not in RISK_LEVELS:
            raise ValueError(f"risk_level must be one of {RISK_LEVELS}")
        return v
