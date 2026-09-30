import os
import glob
import hashlib
from typing import List, Dict, Any
from app.core.logging import get_logger
from app.rag.embeddings import get_embedding_provider, EmbeddingProvider
from app.rag.vector_store import VectorStore, InMemoryVectorStore

log = get_logger(__name__)

def chunk_text(text: str, *, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    if not text:
        return []
    text = text.strip()
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        # try to break at paragraph/newline boundary
        if end < len(text):
            # look back for newline near end
            cut = chunk.rfind("\n")
            if cut > chunk_size * 0.6:
                chunk = chunk[:cut]
                end = start + cut
        chunks.append(chunk.strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + chunk_size - overlap)
    return [c for c in chunks if c]

def ingest_knowledge_dir(*, knowledge_dir: str = "./knowledge", chunk_size: int = 500, overlap: int = 50, embedding_provider: EmbeddingProvider = None, vector_store: VectorStore = None) -> Dict[str, Any]:
    """Ingest approved business knowledge ONLY from knowledge_dir. Never ingest arbitrary email text automatically."""
    from app.core.config import get_settings
    s = get_settings()
    kd = knowledge_dir or s.rag_knowledge_dir
    cs = chunk_size or s.rag_chunk_size
    ov = overlap if overlap is not None else s.rag_chunk_overlap
    emb = embedding_provider or get_embedding_provider()
    vs = vector_store or _get_global_store()

    if not os.path.isdir(kd):
        log.warning(f"Knowledge dir not found: {kd}")
        return {"ingested": 0, "files": 0, "chunks": 0, "dir": kd}

    patterns = [os.path.join(kd, "*.md"), os.path.join(kd, "*.txt"), os.path.join(kd, "*.pdf")]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    files = sorted(set(files))
    total_chunks = 0
    for fp in files:
        try:
            # Only md/txt for now; pdf would need parser
            if fp.endswith(".pdf"):
                log.warning(f"PDF ingestion not implemented, skipping: {fp}")
                continue
            with open(fp, encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            log.error(f"Failed to read {fp}: {e}")
            continue
        chunks = chunk_text(text, chunk_size=cs, overlap=ov)
        for idx, ch in enumerate(chunks):
            chunk_id = hashlib.md5(f"{fp}:{idx}:{ch[:50]}".encode()).hexdigest()[:16]
            meta = {"source": os.path.basename(fp), "path": fp, "chunk_index": idx, "char_len": len(ch), "approved": True}
            vec = emb.embed(ch)
            vs.add(chunk_id=chunk_id, text=ch, embedding=vec, metadata=meta)
            total_chunks += 1
        log.info(f"Ingested {fp}", extra={"extra_data": {"chunks": len(chunks)}})

    return {"ingested": 1, "files": len(files), "chunks": total_chunks, "total_in_store": vs.count(), "dir": kd}

# Global singleton for dev — replaceable via DI
_global_store: InMemoryVectorStore = InMemoryVectorStore()

def _get_global_store() -> InMemoryVectorStore:
    return _global_store

def get_vector_store() -> InMemoryVectorStore:
    return _get_global_store()

def clear_knowledge():
    _get_global_store().clear()
