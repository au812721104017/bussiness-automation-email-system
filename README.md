# AI Email Auto-Reply Agent — Final Release

**Phase 15 — Final Integration, Documentation & Release Checklist**

## Architecture Overview
```
API Layer          → FastAPI `app/main.py`, routers `app/api/*`
Gmail Integration  → `app/services/gmail_service.py` (read-only abstraction), `app/services/gmail_send_service.py` (gate-checked send)
AI/LLM Layer       → `app/services/llm_interface.py` (Mock/OpenAI provider-agnostic)
Classification     → `app/services/classifier.py` (deterministic rules + LLM, injection-safe)
Reply Generation   → `app/services/reply_generator.py` (untrusted data handling, no invention, citations, risk flags)
RAG Knowledge      → `app/rag/*` (chunking 500/50, Mock/OpenAI embeddings, InMemory vector store replaceable, `./knowledge/*.md`)
Policy/Approval    → `app/services/approval_service.py` (PENDING→APPROVED/REJECTED, draft change invalidates, `can_send` gate)
Persistence        → SQLAlchemy SQLite (dev) / Postgres-ready `app/db/*` (agent_runs, email_messages, draft_replies, approvals, audit_events, thread_states, outbound_messages, email_jobs)
Audit Logging      → `app/services/audit_service.py` every state `run_id` with `timestamp, run_id, event_type, status, safe_metadata`
Background Jobs    → `app/jobs/poller.py` + `app/jobs/scheduler.py` (60s interval, dedup, bounded retries, disabled in tests, graceful shutdown)
Observability      → `app/core/logging.py` JSON structured, PII redaction, `app/core/security.py` sanitization/SSRF
Security           → `app/core/rate_limit.py` 60/min, CORS disabled by default, `ADMIN_TOKEN` auth
Tests              → `tests/*` 149 tests (pytest)
```

Data flow: Gmail poll/list → fetch → `normalize_message` → `classify` → `retrieve` (RAG) → `generate` draft → save `draft_replies` with `run_id` → `request_approval` (PENDING) → human approve via `/admin` → `can_send` checks exact `run_id`/`draft_id` + unchanged draft → `GmailSendService.send_approved_draft` → record `outbound_messages` provider ID → audit.

## Setup Guide
1. `git clone` and `cd "C:\bussiness automation email system\Phase1"`
2. `python -m venv venv && venv\Scripts\activate` (Windows) or `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `copy .env.example .env` (Windows) or `cp .env.example .env` — fill placeholders
5. `uvicorn app.main:app --reload --port 8000` — open `http://localhost:8000/health` and `/docs`

## Environment Variables
See `.env.example` (dev) and `.env.production.example` (prod). Key vars:
```
ENV=development|testing|production
LOG_LEVEL=INFO
PORT=8000
DATABASE_URL=sqlite:///./email_automation.db  # prod: postgresql://user:pass@host/email_agent_prod
OPENAI_API_KEY=sk-...  # or leave mocked
GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=...
GMAIL_REDIRECT_URI=http://localhost:8000/gmail/auth/callback
GMAIL_SCOPES=https://www.googleapis.com/auth/gmail.readonly
GMAIL_SEND_SCOPES=https://www.googleapis.com/auth/gmail.send
GMAIL_CREDENTIALS_PATH=./credentials.json
GMAIL_TOKEN_PATH=./token.json
ADMIN_TOKEN=                # dev: empty (warning), prod: required X-Admin-Token
APPROVAL_REQUIRED=true      # never false in prod without explicit risk
SCHEDULER_ENABLED=false
SCHEDULER_INTERVAL_SECONDS=60
RATE_LIMIT_PER_MINUTE=60
CORS_ALLOW_ORIGINS=         # e.g., https://admin.yourdomain.com
SECRET_KEY=change-me
```

## Gmail OAuth Setup
See `GMAIL_SETUP.md` summary:
1. Google Cloud Console → create project → enable Gmail API → OAuth consent (External, add test user) → Credentials → OAuth client Web → redirect `http://localhost:8000/gmail/auth/callback` (prod: `https://yourdomain.com/gmail/auth/callback`).
2. Download `credentials.json` to `GMAIL_CREDENTIALS_PATH`.
3. `uvicorn app.main:app --reload` → `GET /gmail/auth/url` → visit `auth_url` → consent → code exchanged via `google-auth-oauthlib` Flow → `token.json` at `GMAIL_TOKEN_PATH`.
4. Verify `GET /gmail/auth/status` → `authenticated:true`, `GET /gmail/messages` lists.

## Local Run Commands
```
pip install -r requirements.txt
copy .env.example .env   # fill
uvicorn app.main:app --reload --port 8000
# health
curl http://localhost:8000/health
curl http://localhost:8000/ready
# docs
# http://localhost:8000/docs
# orchestrate
curl -X POST http://localhost:8000/orchestrate/run -H "Content-Type: application/json" -d "{\"message_id\":\"test1\",\"sender\":\"a@b.com\",\"subject\":\"Internship\",\"body_text\":\"Need info\"}"
# admin
# http://localhost:8000/admin/dashboard
pytest -v
pytest tests/test_evaluation.py::test_evaluation_report -s
```

## API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (DB) |
| GET | `/docs`, `/openapi.json` | OpenAPI |
| GET | `/gmail/auth/url`, `/gmail/auth/status` | OAuth |
| GET | `/gmail/messages`, `/gmail/messages/{id}`, `/gmail/threads/{id}` | Read-only Gmail (mocked in tests) |
| POST | `/classify` | Intent classification |
| POST | `/drafts/generate`, `GET /drafts`, `GET /drafts/{id}`, `PUT /drafts/{id}`, `GET /drafts/{id}/review` | Drafts (run_id, no send) |
| GET/POST | `/knowledge/ingest`, `/knowledge/search`, `/knowledge/retrieve`, `/knowledge/documents` | RAG |
| POST | `/approvals/request`, `/{id}/approve`, `/{id}/reject`, `GET /approvals`, `/approvals/gate/can-send` | Approval gate |
| POST | `/send`, `GET /send/outbound/{id}` | Gmail send (gate-checked) |
| POST | `/orchestrate/run`, `/orchestrate/run/gmail/{id}`, `/orchestrate/send`, `GET /orchestrate/trace/{run_id}` | Orchestration |
| POST | `/scheduler/trigger`, `GET /scheduler/status`, `POST /scheduler/start`, `POST /scheduler/stop` | Scheduler |
| GET | `/admin`, `/admin/dashboard`, `/admin/overview`, `/admin/emails`, `/admin/runs`, `/admin/drafts`, `/admin/approvals`, `/admin/audits`, `/admin/outbound` | Admin JSON |
| POST | `/admin/drafts/{id}/approve`, `/admin/drafts/{id}/reject`, `/admin/drafts/{id}/regenerate`, `/admin/emails/{id}/ignore` | Admin actions |
| POST | `/email/send` | Legacy queued (approval note) |
| GET | `/schedule/add` | Placeholder scheduler |

## Workflow / State Diagram
```
RECEIVED (Gmail fetch)
  ↓ normalize
NORMALIZED
  ↓ classify
CLASSIFIED (intent, confidence, needs_reply, risk_level, reason, extracted_fields)
  ↓ retrieve (RAG top-k, source metadata)
RETRIEVED
  ↓ generate draft (subject, body, language, tone, confidence, citations, safety_flags)
DRAFTED (saved draft_replies with run_id)
  ↓ request_approval
PENDING_APPROVAL (approval pending, run_id+draft_id binding, audit APPROVAL_REQUESTED)
  ↓ human approve → APPROVED (audit APPROVAL_GRANTED)  or reject → REJECTED
SENDABLE (can_send true only if approved exact draft/run and draft unchanged)
  ↓ send_approved_draft (records outbound_messages provider_message_id, audit SEND_SUCCEEDED)
SENT (audit, run_id traceable)
  Alternative: draft change → invalidated → REJECTED → must re-request

Duplicate prevention: EmailMessage.gmail_message_id unique + poller in-memory cache → skip
Scheduler: poll_once → for each unprocessed → orchestrate → audit SCHEDULER_POLL_COMPLETED
```

## Security Notes
- Secrets via env only, never logged; `StructuredFormatter` redacts `***@domain`, `sk-***`, `ya29.***`, truncates.
- Email content = untrusted data; injection patterns (`ignore previous instructions`, `you are now`, `system:`) flagged, email wrapped as `UNTRUSTED DATA` between markers, never obeyed.
- HTML sanitization `app/core/security.py:sanitize_html` strips `<script>/<iframe>` etc.; links filtered (`javascript:`/`data:` dropped); `has_malicious_attachment` blocks `.exe/.bat/.js`; SSRF `is_ssrf_url` blocks private nets/`localhost`/`metadata.google.internal` (no outbound fetch from email).
- OAuth scopes minimal: `gmail.readonly` (Phase 3) + `gmail.send` (Phase 8) separate.
- Admin auth: `ADMIN_TOKEN` env; `X-Admin-Token` header required if set; dev allows without token with warning, prod requires; `app/api/admin.py` truncates bodies, never exposes tokens/prompts.
- Rate limit `60/min per IP/path` middleware, CORS disabled by default (`CORS_ALLOW_ORIGINS=""`).
- Approval gate `APPROVAL_REQUIRED=true` default; changing draft invalidates; no hidden bypass (grep `can_send`).

## Troubleshooting
| Issue | Fix |
|-------|-----|
| `Configuration error: DATABASE_URL` | Set valid `DATABASE_URL` starting with `sqlite://` or `postgresql://` |
| `Gmail token not found` | Run OAuth flow `GET /gmail/auth/url` → consent → `token.json` |
| `429 Rate limit exceeded` | Wait 60s or raise `RATE_LIMIT_PER_MINUTE` |
| `401 Unauthorized` on `/admin` | Set `ADMIN_TOKEN` and send `X-Admin-Token` header |
| `Approval already approved` on `/admin/drafts/{id}/approve` | Draft already approved; regenerate or use different draft; idempotency returns existing run |
| `DetachedInstanceError` in tests | Ensure draft `out_id` captured before `db.close()` |
| DB `no such table: outbound_messages` | Delete old `email_automation.db` and restart (creates tables), or run migrations |
| RAG `knowledge dir not found` | Ensure `knowledge/*.md` exists; `POST /knowledge/ingest` re-ingests |

## Test Commands
```
pytest -v
# batched (full suite 149 tests ~70s, batched to avoid timeout)
pytest tests/test_config.py tests/test_health.py tests/test_database.py tests/test_logging.py -q
pytest tests/test_gmail.py tests/test_classifier.py tests/test_reply_generator.py tests/test_rag.py -q
pytest tests/test_approval.py tests/test_send.py tests/test_orchestration.py tests/test_scheduler.py -q
pytest tests/test_admin.py tests/test_security.py tests/test_evaluation.py -q
# evaluation report
pytest tests/test_evaluation.py::test_evaluation_report -s
# coverage (if installed)
pytest --cov=app
```

## Production Checklist
See `DEPLOYMENT.md` and `OPERATIONAL_CHECKLIST.md`:
- [ ] `ENV=production`, `DATABASE_URL=postgresql://`, `SECRET_KEY`/`ADMIN_TOKEN` strong random, `OPENAI_API_KEY` set
- [ ] `credentials.json`/`token.json` at `/app/secrets/` `600` perms, HTTPS redirect `https://yourdomain.com/gmail/auth/callback`
- [ ] `APPROVAL_REQUIRED=true` (never false without risk acceptance), no bypass (`grep can_send`)
- [ ] `CORS_ALLOW_ORIGINS=https://admin.yourdomain.com` (not `*`), `RATE_LIMIT_PER_MINUTE=60`
- [ ] Docker `gunicorn -w 2 -k uvicorn.workers.UvicornWorker` behind Nginx HTTPS (see `nginx.conf.example`), health `GET /health` / readiness `GET /ready` probes
- [ ] Backup nightly `pg_dump` + encrypt, knowledge in git
- [ ] Logging JSON ship to CloudWatch/Datadog, monitor `SEND_FAILED` 429 quota (`SCHEDULER_QUERY=in:inbox -in:sent -in:spam -in:trash newer_than:{window}d`, `SCHEDULER_NEWER_THAN_DAYS=2`, interval 300s prod)
- [ ] Run `pytest -q` and `curl /health` before deploy

## Known Limitations
- Mock LLM is heuristic (87.5% on 16-case dataset); real LLM needs human eval; ambiguous `follow_up` vs `support` may misclassify.
- HTML sanitization minimal; complex HTML may lose structure; double-extension attachments not fully checked.
- Rate limiter in-memory, not distributed; SSRF hostname DNS rebinding not fully mitigated.
- Scheduler dedup only by `gmail_message_id`; content hash not checked; poll 60s may miss rapid emails.
- Postgres requires Alembic for migrations; `Base.metadata.create_all` is dev-only.

## Release Checklist (Phase 15)
- [x] App starts, health/ready ok, 8 tables created
- [x] Gmail OAuth documented (`GMAIL_SETUP.md`), read-only + send tested
- [x] Ingestion works (`4` knowledge chunks), classification + RAG + draft + approval gate + send only after approval + dedup + scheduler + audit traceability (`dc114e2f` run with 11 audits)
- [x] No rewrite of working modules unnecessary; gate not weakened
- [x] Docs: arch, setup, env, OAuth, run commands, API, diagram, security, troubleshooting, prod checklist, limitations (this README)
- [x] Tests: 149 passed in 4 batches (see below); no hidden failures

## Test Results
Batched pytest (full suite times out >180s in single run due to scheduler sleeps; verified in batches):
- `tests/test_config.py + test_health.py + test_database.py + test_logging.py`: 25 passed
- `tests/test_gmail.py + test_classifier.py + test_reply_generator.py + test_rag.py`: 52 passed
- `tests/test_approval.py + test_send.py + test_orchestration.py + test_scheduler.py`: 36 passed
- `tests/test_admin.py + test_security.py + test_evaluation.py`: 36 passed
**Total: 149 passed, 0 failed** (warnings: FastAPI `asyncio.iscoroutinefunction` deprecation, httpx `app` shortcut — non-blocking)

All phases 0–14 integrated; no blockers. Production deployment prepared but not executed (per Phase 14 instruction).

