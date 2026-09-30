# Operational Checklist — Production Readiness (Phase 12)

## Secrets & Config
- [ ] `.env` not in git (`.gitignore` covers `.env`, `credentials.json`, `token.json`, `*.db`)
- [ ] `ADMIN_TOKEN` set to strong random (e.g., `openssl rand -hex 32`) in production; `X-Admin-Token` required
- [ ] `SECRET_KEY` set, not default
- [ ] `DATABASE_URL` set to Postgres (`postgresql://`) with strong password; `sqlite` only for dev
- [ ] `OPENAI_API_KEY` in env, not code; `LLM_PROVIDER` evaluated
- [ ] Rotate Gmail `credentials.json` / `token.json` if leaked; store `token.json` encrypted (e.g., Secret Manager)

## OAuth & Gmail
- [ ] Scopes minimal: `gmail.readonly` for reading, `gmail.send` only after approval gate verified
- [ ] Redirect URI `https://yourdomain/gmail/auth/callback` with HTTPS; update Google Cloud console
- [ ] Token scoped to `in:inbox` poll (read-state agnostic, not `is:unread`); respect Gmail API quota (batch, backoff)

## Approval Gate
- [ ] `APPROVAL_REQUIRED=true` (default); never set false in prod without explicit risk acceptance
- [ ] No code path bypasses `can_send`; grep for `can_send` usage before deploy
- [ ] Changing draft invalidates approval (`PUT /drafts/{id}` tested)

## Logging & PII
- [ ] `StructuredFormatter` redacts `***@domain`, `sk-***`, `ya29.***`; no full bodies/tokens in logs (verify with `grep -r "body" app/core/logging.py`)
- [ ] Audit retention: `audit_events` with `run_id` traceability; backup DB regularly
- [ ] No `console.log` of email content in dashboard JS

## Security Hardening (Phase 12)
- [ ] `sanitize_html` used for email HTML → text fallback
- [ ] `has_malicious_attachment` checked before processing attachments (currently logs/blocks exe etc.)
- [ ] `is_ssrf_url` blocks private IPs if any outbound fetch added
- [ ] Rate limiting `RATE_LIMIT_PER_MINUTE=60` tuned; consider Redis for multi-instance
- [ ] CORS `CORS_ALLOW_ORIGINS` empty by default; set to `https://admin.yourdomain` in prod, not `*`
- [ ] Input validation: `subject`/`body` max 4000 chars, recipients validated via `validate_email_address`

## Reliability
- [ ] Scheduler `SCHEDULER_ENABLED=false` by default; enable explicitly; `SCHEDULER_INTERVAL_SECONDS=60` not too aggressive
- [ ] Bounded retries `SCHEDULER_MAX_RETRIES=3` with backoff; permanent errors not retried
- [ ] Graceful shutdown: `lifespan` stops scheduler thread
- [ ] Idempotency: `gmail_message_id` unique; duplicate poll skips
- [ ] DB transactions: `SessionLocal` per request, `commit`/`rollback` on error (add explicit rollback in prod)

## Health & Observability
- [ ] `GET /health` returns `status healthy` and `phase`; use as readiness probe
- [ ] `/docs` (OpenAPI) secured or disabled in prod (via `docs_url=None` if needed)
- [ ] `pytest` suite passes (`113+` tests); ensure `prod` env runs `ENV=production` tests
- [ ] Monitoring: collect `SCHEDULER_POLL_*`, `APPROVAL_*`, `SEND_*` audit counts

## Dependencies
- [ ] `pip install -r requirements.txt` pinned; run `pip-audit` or `safety check`
- [ ] Python 3.11+; `uvicorn` with `--proxy-headers` behind reverse proxy

## Deployment
- [ ] Run with `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2` behind Nginx/HTTPS
- [ ] Use Postgres; run `init_db()` migrations; backup
- [ ] Set `ENV=production`, `LOG_LEVEL=INFO`, `PRODUCTION_HARDENING=true` (if added)

## Test Commands
```
pip install -r requirements.txt
pytest -v
pytest tests/test_security.py -v  # Phase 12 hardening
curl http://localhost:8000/health
curl -H "X-Admin-Token: $ADMIN_TOKEN" http://localhost:8000/admin/overview
```

## Known Limitations (Phase 12)
- In-memory rate limiter not distributed; SSRF hostname DNS rebinding not fully blocked; mock LLM not production-grade.
