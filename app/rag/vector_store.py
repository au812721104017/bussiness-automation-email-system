from typing import List, Dict, Any, Optional
import math
from abc import ABC, abstractmethod

class VectorStore(ABC):
    @abstractmethod
    def add(self, chunk_id: str, text: str, embedding: List[float], metadata: Dict[str, Any]): pass
    @abstractmethod
    def search(self, query_embedding: List[float], top_k: int = 3) -> List[Dict[str, Any]]: pass
    @abstractmethod
    def clear(self): pass
    @abstractmethod
    def count(self) -> int: pass

def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

class InMemoryVectorStore(VectorStore):
    """Replaceable local fallback — keeps source metadata for traceability. Swappable with FAISS/Chroma/PGVector later."""
    def __init__(self):
        self._store: List[Dict[str, Any]] = []

    def add(self, chunk_id: str, text: str, embedding: List[float], metadata: Dict[str, Any]):
        self._store.append({"chunk_id": chunk_id, "text": text, "embedding": embedding, "metadata": metadata})

    def search(self, query_embedding: List[float], top_k: int = 3) -> List[Dict[str, Any]]:
        scored = []
        for item in self._store:
            score = _cosine(query_embedding, item["embedding"])
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, item in scored[:top_k]:
            results.append({"chunk_id": item["chunk_id"], "text": item["text"], "score": score, "metadata": item["metadata"]})
        return results

    def clear(self):
        self._store.clear()

    def count(self) -> int:
        return len(self._store)
