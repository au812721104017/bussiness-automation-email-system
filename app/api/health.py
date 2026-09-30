from fastapi import APIRouter
from datetime import datetime, timezone
from app.core.config import get_settings
from app.db.session import SessionLocal

router = APIRouter()

@router.get("/health", tags=["health"], summary="Health check (liveness)")
async def health_check():
    settings = get_settings()
    return {
        "status": "healthy",
        "phase": settings.phase,
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.env,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": f"FastAPI running ({settings.phase}) — approval gate enforced",
        "checks": {
            "api": "ok",
            "config": "loaded",
            "logging": "structured",
            "approval_gate": "enabled" if settings.approval_required else "disabled",
        },
    }

@router.get("/ready", tags=["health"], summary="Readiness check (DB + config)")
async def readiness_check():
    settings = get_settings()
    db_ok = False
    db_error = None
    try:
        from sqlalchemy import text
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception as e:
        db_error = str(e)[:200]
    ready = db_ok
    return {
        "ready": ready,
        "phase": settings.phase,
        "env": settings.env,
        "checks": {
            "db": "ok" if db_ok else f"failed: {db_error}",
            "config": "ok",
            "approval_gate": "enabled" if settings.approval_required else "disabled",
        },
    }

@router.get("/", tags=["health"], include_in_schema=False)
async def root():
    return {"message": "AI Email Auto-Reply Agent — see /health, /ready and /docs"}
