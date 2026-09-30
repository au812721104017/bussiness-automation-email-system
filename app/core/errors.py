import logging
from fastapi import Request, FastAPI
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
import traceback

logger = logging.getLogger("ai_email_agent")

def add_error_handlers(app: FastAPI):
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        logger.warning(f"HTTP {exc.status_code} on {request.method} {request.url.path}: {exc.detail}", extra={"extra_data": {"path": request.url.path, "status_code": exc.status_code}})
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "status_code": exc.status_code,
                "detail": exc.detail,
                "path": request.url.path,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.warning(f"Validation error on {request.url.path}: {exc.errors()}")
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "status_code": 422,
                "detail": exc.errors(),
                "path": request.url.path,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled error on {request.method} {request.url.path}: {str(exc)[:300]}", exc_info=True)
        # Never leak internal details; prod returns safe message, dev also safe (Phase 12)
        from app.core.config import get_settings
        s = get_settings()
        detail = "An unexpected error occurred. Check server logs."
        if s.env == "development":
            # Still safe but slightly more detail for local debugging, truncated
            detail = str(exc)[:300]
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "status_code": 500,
                "detail": detail,
                "path": request.url.path,
            },
        )
