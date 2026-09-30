import os
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

load_dotenv()

from app.core.config import get_settings
from app.core.logging import setup_logging, get_logger
from app.core.errors import add_error_handlers
from app.api.health import router as health_router

# --- Logging & Settings ---
try:
    settings = get_settings()
except Exception as e:
    import sys
    print(f"Configuration error: {e}", file=sys.stderr)
    raise

logger = setup_logging(settings.log_level)
log = get_logger(__name__)

# --- DB (Phase 2: replaceable SQLAlchemy SQLite/Postgres, repository pattern) ---
try:
    from app.db.session import engine, SessionLocal, init_db
    from app.db.models import EmailJob, AgentRun, EmailMessage, DraftReply, AuditEvent
    init_db()
    DB_AVAILABLE = True
    log.info("Phase 2 DB initialized (email_messages, agent_runs, draft_replies, approvals, audit_events, thread_states)")
except Exception as e:
    log.warning(f"DB not available: {e}")
    DB_AVAILABLE = False
    engine = None
    SessionLocal = None
    EmailJob = None  # type: ignore

# --- Optional OpenAI (lazy, not required for /health) ---
openai_client = None
try:
    from openai import OpenAI
    if settings.openai_api_key and settings.openai_api_key != "sk-test-your-key-here":
        openai_client = OpenAI(api_key=settings.openai_api_key)
    else:
        log.info("OpenAI client not configured (Phase 2: LLM not required)")
except Exception as e:
    log.warning(f"OpenAI init skipped: {e}")
    openai_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"Starting {settings.app_name} v{settings.app_version} phase={settings.phase} env={settings.env}")
    # Scheduler disabled by default in tests; start only if enabled
    if settings.scheduler_enabled:
        try:
            from app.jobs.scheduler import get_scheduler
            get_scheduler().start()
            log.info("Scheduler auto-started from lifespan")
        except Exception as e:
            log.warning(f"Scheduler auto-start failed: {e}")
    yield
    # Graceful shutdown
    try:
        from app.jobs.scheduler import get_scheduler
        get_scheduler().stop()
    except Exception:
        pass
    log.info("Shutting down gracefully")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI Email Auto-Reply Agent — Phase 12: Security & Reliability Hardening. Approval gate enforced; PII-safe, no bypass.",
    lifespan=lifespan,
)

# CORS — safe default: no origins unless configured (Phase 12)
try:
    from fastapi.middleware.cors import CORSMiddleware
    allow_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()] if settings.cors_allow_origins else []
    if allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=allow_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT"], allow_headers=["*"])
        log.info(f"CORS enabled for: {allow_origins}")
    else:
        log.info("CORS disabled by default (set CORS_ALLOW_ORIGINS to enable)")
except Exception as e:
    log.warning(f"CORS setup failed: {e}")

# Rate limiting — simple per-IP per-path (Phase 12)
try:
    from fastapi import Request
    from app.core.rate_limit import RateLimiter
    _rl = RateLimiter(per_minute=settings.rate_limit_per_minute)
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        # Exclude health/docs
        if request.url.path in ("/health", "/docs", "/openapi.json", "/redoc", "/admin/dashboard"):
            return await call_next(request)
        try:
            _rl.check(request.client.host + ":" + request.url.path if request.client else "unknown:" + request.url.path)
        except Exception as e:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=429, content={"error": "rate_limited", "detail": str(e)})
        return await call_next(request)
    log.info(f"Rate limiting enabled: {settings.rate_limit_per_minute}/min per IP/path")
except Exception as e:
    log.warning(f"Rate limit setup failed: {e}")

add_error_handlers(app)
app.include_router(health_router)
# Gmail read-only (Phase 3) — abstraction, no send
try:
    from app.api.gmail import router as gmail_router
    app.include_router(gmail_router)
    log.info("Gmail read-only routes registered (Phase 3)")
except Exception as e:
    log.warning(f"Gmail routes not registered: {e}")
# Classification (Phase 4) — no Gmail/send calls
try:
    from app.api.classify import router as classify_router
    app.include_router(classify_router)
    log.info("Classification routes registered (Phase 4)")
except Exception as e:
    log.warning(f"Classify routes not registered: {e}")
# Draft generation (Phase 5) — stored with run_id, never sent
try:
    from app.api.drafts import router as drafts_router
    app.include_router(drafts_router)
    log.info("Draft routes registered (Phase 5)")
except Exception as e:
    log.warning(f"Draft routes not registered: {e}")
# RAG knowledge (Phase 6) — approved docs only, replaceable vector store
try:
    from app.api.knowledge import router as knowledge_router
    app.include_router(knowledge_router)
    # Auto-ingest approved knowledge on startup (local fallback)
    from app.rag.ingestion import ingest_knowledge_dir
    res = ingest_knowledge_dir()
    log.info(f"RAG knowledge ingested (Phase 6): {res.get('chunks',0)} chunks from {res.get('files',0)} files")
except Exception as e:
    log.warning(f"Knowledge routes not registered: {e}")
# Approval gate (Phase 7) — mandatory, no bypass, draft change invalidates
try:
    from app.api.approvals import router as approvals_router
    app.include_router(approvals_router)
    log.info(f"Approval gate registered (Phase 7) — required={settings.approval_required}")
except Exception as e:
    log.warning(f"Approvals not registered: {e}")
# Gmail send (Phase 8) — behind approval gate, records provider ID, no bypass
try:
    from app.api.send import router as send_router
    app.include_router(send_router)
    log.info("Gmail send registered (Phase 8) — gate-checked")
except Exception as e:
    log.warning(f"Send not registered: {e}")
# Orchestration (Phase 9) — state machine, idempotent, run_id traceable
try:
    from app.api.orchestrate import router as orch_router
    app.include_router(orch_router)
    log.info("Orchestration registered (Phase 9) — state machine")
except Exception as e:
    log.warning(f"Orchestration not registered: {e}")
# Scheduler (Phase 10) — polling, dedup, retry, graceful shutdown, never bypass gate
try:
    from app.api.scheduler import router as scheduler_router
    app.include_router(scheduler_router)
    log.info("Scheduler registered (Phase 10)")
except Exception as e:
    log.warning(f"Scheduler not registered: {e}")
# Admin API + Dashboard (Phase 11) — safe review, no secrets, temp auth for dev
try:
    from app.api.admin import router as admin_router
    app.include_router(admin_router)
    log.info("Admin dashboard registered (Phase 11)")
except Exception as e:
    log.warning(f"Admin not registered: {e}")


@app.get("/ai/query", tags=["ai"], summary="AI query (optional, Phase 5)")
async def ai_query(message: str):
    if openai_client is None:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured. Set OPENAI_API_KEY in .env")
    log.info("AI query requested", extra={"extra_data": {"message_len": len(message)}})
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": message}],
        )
        return {"response": response.choices[0].message.content}
    except Exception as e:
        log.error(f"OpenAI error: {str(e)[:200]}")
        raise HTTPException(status_code=500, detail=str(e)[:500])


@app.post("/email/send", tags=["email"], summary="Queue email via repository (Phase 2, approval gate in Phase 7)")
async def send_email(to: str, subject: str, body: str):
    if not DB_AVAILABLE or SessionLocal is None:
        raise HTTPException(status_code=500, detail="Database not available. Configure DATABASE_URL")
    if "@" not in to or "." not in to:
        raise HTTPException(status_code=422, detail="Invalid email address")
    from sqlalchemy.orm import Session as SASession
    with SASession(engine) as session:
        job = EmailJob(to_email=to, subject=subject, body=body, status="queued")
        session.add(job)
        session.commit()
        session.refresh(job)
    log.info("Email queued (not sent, approval required from Phase 7)", extra={"extra_data": {"job_id": job.id, "to_domain": to.split("@")[-1]}})
    return {"status": "queued", "job_id": job.id, "to": to, "subject": subject, "note": "Approval gate required before actual send (Phase 7)"}


@app.get("/schedule/add", tags=["schedule"], summary="Schedule placeholder (Phase 10)")
async def add_schedule(to: str, subject: str, body: str, run_at: str):
    log.info("Schedule request (placeholder)", extra={"extra_data": {"run_at": run_at}})
    return {"status": "scheduled_placeholder", "run_at": run_at, "detail": "Install apscheduler for real scheduling (Phase 10)"}
