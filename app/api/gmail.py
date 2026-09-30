from typing import Optional
import os
from fastapi import APIRouter, HTTPException, Query, Depends
from app.services.gmail_service import GmailService
from app.core.logging import get_logger
from google_auth_oauthlib.flow import Flow

log = get_logger(__name__)
router = APIRouter(prefix="/gmail", tags=["gmail"])

def get_gmail_service() -> GmailService:
    return GmailService()

@router.get("/auth/url", summary="Get Gmail OAuth URL (read-only scope)")
async def gmail_auth_url(service: GmailService = Depends(get_gmail_service)):
    result = service.get_auth_url()
    if not result.get("configured"):
        raise HTTPException(status_code=400, detail=result.get("detail", "Gmail not configured"))
    return result

@router.get("/auth/status", summary="Check Gmail auth status")
async def gmail_auth_status(service: GmailService = Depends(get_gmail_service)):
    return {
        "configured": service.is_configured(),
        "authenticated": service.is_authenticated(),
        "scopes": service._settings.gmail_scopes,
        "note": "Tokens stored locally at GMAIL_TOKEN_PATH; never logged",
    }

@router.get("/auth/callback", summary="Gmail OAuth callback — exchange code for tokens")
async def gmail_auth_callback(code: str = Query(...), service: GmailService = Depends(get_gmail_service)):
    if not service.is_configured():
        raise HTTPException(status_code=400, detail="Gmail not configured — set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET")
    try:
        from google.oauth2.credentials import Credentials
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": service._settings.gmail_client_id,
                    "client_secret": service._settings.gmail_client_secret,
                    "redirect_uris": [service._settings.gmail_redirect_uri],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=[s.strip() for s in service._settings.gmail_scopes.split(",") if s.strip()] or ["https://www.googleapis.com/auth/gmail.readonly"],
        )
        flow.redirect_uri = service._settings.gmail_redirect_uri
        flow.fetch_token(code=code)
        creds = flow.credentials
        # Save credentials to token path
        token_path = service._settings.gmail_token_path
        os.makedirs(os.path.dirname(os.path.abspath(token_path)), exist_ok=True)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        log.info("Gmail credentials saved to token file", extra={"extra_data": {"token_path": token_path}})
        return {"status": "ok", "message": "Gmail OAuth credentials saved successfully"}
    except Exception as e:
        log.error(f"Gmail OAuth callback error: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)[:200]}")

@router.get("/messages", summary="List Gmail messages (read-only, mocked if not authenticated)")
async def list_messages(q: Optional[str] = Query(None, description="Gmail query"), max_results: int = Query(10, ge=1, le=100), service: GmailService = Depends(get_gmail_service)):
    try:
        msgs = service.list_messages(query=q, max_results=max_results)
        return {"messages": msgs, "count": len(msgs), "q": q}
    except RuntimeError as e:
        # In testing or without auth, return graceful error (no token leak)
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/messages/{message_id}", summary="Fetch & normalize Gmail message")
async def get_message(message_id: str, service: GmailService = Depends(get_gmail_service)):
    try:
        raw = service.get_message(message_id)
        normalized = service.normalize_message(raw)
        return {"raw_id": raw.get("id"), "normalized": normalized.dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

@router.get("/threads/{thread_id}", summary="Fetch Gmail thread")
async def get_thread(thread_id: str, service: GmailService = Depends(get_gmail_service)):
    try:
        thread = service.get_thread(thread_id)
        # Normalize each message in thread if present
        messages = thread.get("messages", [])
        normalized_list = []
        for m in messages[:10]:  # cap for safety
            try:
                normalized_list.append(service.normalize_message(m).dict())
            except Exception:
                continue
        return {"thread_id": thread.get("id"), "history_id": thread.get("historyId"), "message_count": len(messages), "messages": thread.get("messages"), "normalized_preview": normalized_list}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
