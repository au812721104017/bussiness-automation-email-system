from typing import List, Optional, Dict
from datetime import datetime
from pydantic import BaseModel, Field

class NormalizedEmail(BaseModel):
    """Internal schema — Gmail details normalized, safe to store/log subset."""
    message_id: str = Field(..., description="Gmail message ID")
    thread_id: Optional[str] = None
    sender: str = Field(..., description="From header raw")
    sender_email: Optional[str] = None  # extracted email
    recipients: List[str] = Field(default_factory=list, description="To list")
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    snippet: Optional[str] = None
    internal_date: Optional[int] = None  # gmail internalDate ms
    received_at: Optional[datetime] = None
    headers: Dict[str, str] = Field(default_factory=dict)
    size_estimate: Optional[int] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
