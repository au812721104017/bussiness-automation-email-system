from typing import List, Dict, Any
from app.core.logging import get_logger
from app.rag.embeddings import get_embedding_provider, EmbeddingProvider
from app.rag.vector_store import VectorStore, InMemoryVectorStore
from app.rag.ingestion import get_vector_store

log = get_logger(__name__)

class RetrievalService:
    def __init__(self, embedding_provider: EmbeddingProvider = None, vector_store: VectorStore = None, top_k: int = None):
        from app.core.config import get_settings
        s = get_settings()
        self.emb = embedding_provider or get_embedding_provider()
        self.store = vector_store or get_vector_store()
        self.top_k = top_k if top_k is not None else s.rag_top_k

    def retrieve(self, query: str, *, top_k: int = None, min_score: float = 0.0) -> List[Dict[str, Any]]:
        if not query or not query.strip():
            return []
        k = top_k if top_k is not None else self.top_k
        q_vec = self.emb.embed(query)
        results = self.store.search(q_vec, top_k=k)
        # Filter low scores
        filtered = [r for r in results if r["score"] >= min_score]
        # Traceability: log source counts, not full text
        log.info("RAG retrieve", extra={"extra_data": {"query_len": len(query), "top_k": k, "returned": len(filtered), "sources": list({r["metadata"].get("source") for r in filtered})}})
        # Every result retains source metadata for traceability
        return filtered

    def retrieve_as_context(self, query: str, *, top_k: int = None) -> Dict[str, Any]:
        hits = self.retrieve(query, top_k=top_k)
        context = "\n\n".join([f"[Source: {h['metadata'].get('source')} | score {h['score']:.3f}]\n{h['text']}" for h in hits])
        sources = [h["metadata"] for h in hits]
        return {"context": context, "sources": sources, "hits": hits}
