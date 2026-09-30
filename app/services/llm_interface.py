import json
from typing import Dict, Any, Protocol, Optional
from abc import ABC, abstractmethod
from app.core.logging import get_logger

log = get_logger(__name__)

class LLMProvider(ABC):
    """Provider-agnostic interface — replaceable. Never allow direct Gmail/send calls."""
    @abstractmethod
    def generate_json(self, system_prompt: str, user_prompt: str, *, model: Optional[str] = None) -> Dict[str, Any]:
        """Return parsed JSON dict. Must not execute tool calls."""
        raise NotImplementedError

class MockLLMProvider(LLMProvider):
    """Deterministic mock for dev/tests — no network."""
    def __init__(self, fixed_response: Optional[Dict[str, Any]] = None):
        self.fixed_response = fixed_response
        self.calls = []

    def generate_json(self, system_prompt: str, user_prompt: str, *, model: Optional[str] = None) -> Dict[str, Any]:
        self.calls.append({"system": system_prompt, "user": user_prompt, "model": model})
        if self.fixed_response is not None:
            return self.fixed_response
        # Simple heuristic fallback based on user_prompt content (email body)
        text = user_prompt.lower()
        # This is only for mock to behave reasonably when classifier fallback uses LLM
        if any(k in text for k in ["unsubscribe", "sale", "% off", "marketing", "newsletter"]):
            return {"intent": "spam_marketing", "confidence": 0.92, "needs_reply": False, "risk_level": "low", "reason": "Marketing/spam keywords detected", "extracted_fields": {}}
        if any(k in text for k in ["internship", "course", "training", "enroll"]):
            return {"intent": "internship_course_inquiry", "confidence": 0.88, "needs_reply": True, "risk_level": "low", "reason": "Internship/course keywords", "extracted_fields": {"topic": "internship"}}
        if any(k in text for k in ["meeting", "schedule", "calendar", "available"]):
            return {"intent": "meeting_request", "confidence": 0.85, "needs_reply": True, "risk_level": "low", "reason": "Meeting keywords", "extracted_fields": {}}
        if any(k in text for k in ["price", "pricing", "cost", "quote", "how much"]):
            return {"intent": "pricing_request", "confidence": 0.86, "needs_reply": True, "risk_level": "low", "reason": "Pricing keywords", "extracted_fields": {}}
        if any(k in text for k in ["help", "support", "issue", "broken", "error", "question"]):
            return {"intent": "support_question", "confidence": 0.84, "needs_reply": True, "risk_level": "low", "reason": "Support keywords", "extracted_fields": {}}
        if any(k in text for k in ["confidential", "legal", "lawsuit", "payment", "refund", "urgent"]):
            return {"intent": "sensitive_high_risk", "confidence": 0.9, "needs_reply": True, "risk_level": "high", "reason": "Sensitive/high-risk keywords", "extracted_fields": {}}
        if len(text.strip()) < 10:
            return {"intent": "unknown", "confidence": 0.5, "needs_reply": False, "risk_level": "low", "reason": "Empty or very short content", "extracted_fields": {}}
        return {"intent": "unknown", "confidence": 0.55, "needs_reply": False, "risk_level": "low", "reason": "No clear intent", "extracted_fields": {}}

class OpenAILLMProvider(LLMProvider):
    """Real provider — only used when OPENAI_API_KEY configured and llm_provider=openai. Wraps chat completion with JSON mode."""
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key)
        except Exception as e:
            raise RuntimeError(f"OpenAI init failed: {e}") from e

    def generate_json(self, system_prompt: str, user_prompt: str, *, model: Optional[str] = None) -> Dict[str, Any]:
        m = model or self.model
        # Never expose secrets; log only lengths
        log.info("LLM generate_json", extra={"extra_data": {"model": m, "system_len": len(system_prompt), "user_len": len(user_prompt)}})
        try:
            resp = self.client.chat.completions.create(
                model=m,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            parsed = json.loads(content)
            # Ensure dict
            if not isinstance(parsed, dict):
                raise ValueError("LLM returned non-dict")
            return parsed
        except Exception as e:
            log.error(f"LLM generate failed: {type(e).__name__}")
            raise RuntimeError(f"LLM call failed: {str(e)[:300]}") from e

def get_llm_provider() -> LLMProvider:
    from app.core.config import get_settings
    s = get_settings()
    if s.llm_provider == "openai" and s.openai_api_key and s.openai_api_key != "sk-test-your-key-here":
        try:
            return OpenAILLMProvider(api_key=s.openai_api_key, model=s.llm_model)
        except Exception as e:
            log.warning(f"OpenAI provider unavailable, falling back to mock: {e}")
            return MockLLMProvider()
    return MockLLMProvider()
