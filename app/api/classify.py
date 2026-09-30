from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.schemas.email import NormalizedEmail
from app.schemas.classification import ClassificationResult
from app.services.classifier import get_classifier, ClassifierService

router = APIRouter(prefix="/classify", tags=["classification"])

class ClassifyRequest(BaseModel):
    message_id: str = "test"
    sender: str = "user@example.com"
    subject: Optional[str] = None
    body_text: Optional[str] = None
    snippet: Optional[str] = None
    recipients: list = []

def get_cls() -> ClassifierService:
    return get_classifier()

@router.post("", response_model=ClassificationResult, summary="Classify email intent (no Gmail/send calls)")
async def classify_email(req: ClassifyRequest, svc: ClassifierService = Depends(get_cls)):
    try:
        email = NormalizedEmail(message_id=req.message_id, sender=req.sender, subject=req.subject, body_text=req.body_text, snippet=req.snippet, recipients=req.recipients)
        result = svc.classify(email)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:400])
