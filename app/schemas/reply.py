from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, validator

class ReplyGenerationResult(BaseModel):
    subject: Optional[str] = Field(None, description="Suggested subject (if reply needs new subject)")
    body: str = Field(..., description="Reply body, professional tone")
    language: str = Field("en", description="ISO language code")
    tone: str = Field("professional", description="Tone descriptor")
    confidence: float = Field(..., ge=0.0, le=1.0)
    citations: List[str] = Field(default_factory=list, description="Source references if knowledge used")
    safety_flags: Dict[str, Any] = Field(default_factory=dict, description="Risk flags: needs_review, sensitive, missing_info, etc")
    risk_level: str = Field("low", description="low/medium/high")

    @validator("risk_level")
    def _valid_risk(cls, v):
        if v not in ("low", "medium", "high"):
            raise ValueError("risk_level must be low/medium/high")
        return v

    @validator("body")
    def _body_not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("body must not be empty")
        if len(v) > 20000:
            raise ValueError("body too long")
        return v
