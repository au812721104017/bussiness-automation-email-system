from fastapi import APIRouter, Depends, Query
from typing import Optional
from sqlalchemy.orm import Session
from app.rag.ingestion import ingest_knowledge_dir, get_vector_store, clear_knowledge
from app.rag.retrieval import RetrievalService
from app.db.session import get_db
from app.services.run_service import generate_run_id, create_run
from app.services.audit_service import create_audit_event

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

@router.post("/ingest", summary="Ingest approved knowledge from knowledge dir (no email text auto-trusted)")
async def ingest():
    result = ingest_knowledge_dir()
    return result

@router.post("/clear", summary="Clear knowledge store (dev only)")
async def clear():
    clear_knowledge()
    return {"cleared": True, "count": get_vector_store().count()}

@router.get("/search", summary="Retrieve top-k knowledge chunks for a query")
async def search(q: str = Query(..., description="Query text"), top_k: int = Query(None, ge=1, le=10)):
    svc = RetrievalService(top_k=top_k) if top_k else RetrievalService()
    hits = svc.retrieve(q, top_k=top_k)
    # Return safe metadata only, not full embeddings
    return {"query": q, "top_k": svc.top_k, "hits": [{"text": h["text"][:800], "score": h["score"], "metadata": h["metadata"]} for h in hits]}

@router.get("/documents", summary="List ingestion stats")
async def documents():
    vs = get_vector_store()
    return {"count": vs.count(), "knowledge_dir": "./knowledge"}

@router.get("/retrieve", summary="Retrieve as LLM context (with citations)")
async def retrieve_as_context(q: str = Query(...), top_k: int = Query(3, ge=1, le=10)):
    svc = RetrievalService(top_k=top_k)
    ctx = svc.retrieve_as_context(q, top_k=top_k)
    return {"query": q, "context": ctx["context"][:4000], "sources": ctx["sources"]}
