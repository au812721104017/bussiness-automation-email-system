from typing import List
from abc import ABC, abstractmethod
import hashlib

class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> List[float]:
        raise NotImplementedError
    def embed_many(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic local fallback — hash-based bag-of-words style vector (replaceable). Not semantic, but deterministic for tests/dev."""
    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        # Simple TF-like: hash each word into dim buckets
        vec = [0.0] * self.dim
        if not text:
            return vec
        words = text.lower().split()
        for w in words:
            # hash to bucket
            h = int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim
            vec[h] += 1.0
        # L2 normalize
        norm = sum(x*x for x in vec) ** 0.5
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self.api_key = api_key
        self.model = model
        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=api_key)
        except Exception as e:
            raise RuntimeError(f"OpenAI embeddings init failed: {e}") from e

    def embed(self, text: str) -> List[float]:
        resp = self.client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding

def get_embedding_provider() -> EmbeddingProvider:
    from app.core.config import get_settings
    s = get_settings()
    if s.rag_embedding_provider == "openai" and s.openai_api_key and s.openai_api_key != "sk-test-your-key-here":
        try:
            return OpenAIEmbeddingProvider(api_key=s.openai_api_key)
        except Exception:
            pass
    return MockEmbeddingProvider()
