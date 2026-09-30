# Architecture Baseline — Phase 0

## Goal
Production-oriented AI email assistant: `Gmail → classify → RAG → draft → approval gate → send`.

## Layers (Phase 0 Baseline)
```
API Layer (FastAPI)         -> app/main.py, app/api/*
Gmail Integration           -> app/services/gmail_service.py (abstraction, Phase 3+)
AI/LLM Layer                -> app/services/llm_service.py (provider-agnostic, Phase 4+)
Classification              -> app/services/classifier.py (Phase 4)
Reply Generation            -> app/services/reply_generator.py (Phase 5)
Policy / Approval           -> app/services/approval_service.py (Phase 7)
Persistence                 -> app/db/*, SQLAlchemy SQLite (dev) / Postgres (prod) — Phase 2
Audit Logging               -> app/core/audit.py — every state transition has run_id
Background Jobs / Scheduler -> app/jobs/scheduler.py (Phase 10)
Observability               -> app/core/logging.py (structured JSON), /health
Tests                       -> tests/*, pytest (Phase 1+)
```

## Data Flow (Phase 1 → 15)
1. Background poller or webhook fetches email → normalize → `run_id` generated (uuid4)
2. Classification (intent, confidence, needs_reply, risk_level)
3. RAG retrieval (top-k, source metadata retained)
4. Draft generation (subject, body, tone, citations, flags) — stored with `run_id`, **not sent**
5. Approval gate `PENDING_APPROVAL → APPROVED/REJECTED` — draft change invalidates approval
6. Send service validates approval belongs to exact draft/run before Gmail send
7. Every transition emits `audit_event {timestamp, run_id, event_type, status, metadata}`

## Safety Boundaries (Critical)
- NO code path sends without valid `approval` for exact `draft`+`run_id` and unchanged draft.
- Secrets via env vars only; never logged. Tokens redacted.
- Email content = untrusted data; prompt-injection cannot override system instructions.
- Scheduling disabled by default in tests; approval enabled by default.

## Run IDs & Audit (Phase 2)
- `run_id: uuid4` per agent execution; propagated via logging `record.run_id` and DB columns.
- `audit_events` table: id, run_id, event_type, status, metadata (JSON, safe), created_at.

## Persistence Strategy (Phase 2)
- SQLAlchemy declarative, repository/service pattern (no DB logic in routes).
- Migration: `Base.metadata.create_all` for dev; Alembic-ready for prod.

## Config & Secrets (Phase 1)
- `app/core/config.py` Settings from env (pydantic BaseSettings). Validation fails loudly.
- `.env.example` placeholders only; `.env` gitignored.

## Current Phase Status
- Phase 0: established (this file)
- Phase 1: implemented — /health, /docs, structured logging, error handling, Settings validation
- Phase 2: implemented — SQLAlchemy SQLite (Postgres-ready), models: email_messages, agent_runs (run_id uuid), draft_replies, approvals, audit_events, thread_states (+ email_jobs compat), repository pattern, run_id traceability, every state → audit event
- Phase 3: implemented — Gmail OAuth read-only abstraction (`app/services/gmail_service.py`), NormalizedEmail (`app/schemas/email.py`), routes `/gmail/auth/*`, `/gmail/messages`, `/gmail/threads`, min scope `gmail.readonly`, no send, mocked tests
- Phase 4: implemented — provider-agnostic LLM (`app/services/llm_interface.py` MockLLM/OpenAI), classifier (`app/services/classifier.py`) deterministic rules + structured JSON `intent,confidence,needs_reply,risk_level,reason,extracted_fields`, injection-safe, `POST /classify`, 16 tests
- Phase 5: implemented — reply generator (`app/services/reply_generator.py`) strong system instructions (untrusted data, no invention, human review, professional tone), structured `subject,body,language,tone,confidence,citations,safety_flags,risk_level`, draft stored with `run_id` + `DRAFT_CREATED` audit, `POST /drafts/generate`, 12 tests (mocked LLM, malformed, injection)
- Phase 6: implemented — RAG (`app/rag/embeddings.py` Mock/OpenAI, `app/rag/vector_store.py` InMemory replaceable, `app/rag/ingestion.py` chunking, `app/rag/retrieval.py` top-k), `knowledge/` (company_profile, services, internship_faq, pricing_policy), `/knowledge/*` search/retrieve, draft auto-retrieves with citations + source traceability, no email-as-knowledge, 12 tests
- Phase 7: implemented — approval gate (`app/services/approval_service.py` flow RECEIVED→PENDING→APPROVED/REJECTED→SENDABLE, explicit binding run_id+draft_id, no reuse, draft change invalidates, default enabled, `can_send` gate, audit every transition), `/approvals/*` + `/drafts/{id}/review` + `PUT /drafts/{id}` invalidation, `GET /approvals/gate/can-send`, 13 tests (blocked without, wrong run, modified draft, valid, audit)
- Phase 8: implemented — Gmail send (`app/services/gmail_send_service.py` gate-checked, validates recipients/thread, reply-in-thread, records provider_message_id/sent_at/failed, never logs tokens/bodies), `app/db/models.py:OutboundMessage`, `POST /send` + `GET /send/outbound/{id}`, 8 mocked tests (no approval→403, wrong run→403, modified→403, valid→sent+provider ID, failure→audited), critical rule enforced via `can_send`
- Phase 9: implemented — orchestration (`app/services/orchestrator.py` state machine 1 fetch→9 audit, run_id traceable, resumable via AgentRun status, idempotent duplicate prevention via gmail_message_id/thread, `app/api/orchestrate.py` /run, /run/gmail/{id}, /send, /trace/{run_id}), 7 e2e tests with mocked Gmail/LLM
- Phase 10: implemented — scheduler (`app/jobs/poller.py` eligible/dedup/bounded transient retry/no permanent retry/audit, `app/jobs/scheduler.py` configurable interval, disabled by default in tests, graceful start/stop via threading, lifespan integration), `app/api/scheduler.py` status/start/stop/trigger/polls, 8 tests (polling, dedup, transient retry, permanent no-retry, disabled, graceful, never bypass gate, audit)
- Phase 11: implemented — admin API (`app/api/admin.py` overview/emails/runs/drafts/audits with safe truncated fields, no secrets/tokens/prompts, dashboard at `/admin/dashboard` simple HTML with JS, actions approve/reject/regenerate/ignore via `app/services/approval_service.py`), temp auth via `ADMIN_TOKEN` (`X-Admin-Token` header, dev allows without token with warning, production requires), 10 tests (review + actions + safe fields)
- Phase 12: implemented — hardening (`app/core/security.py` HTML sanitization, SSRF, link/attachment checks, PII redaction; `app/core/rate_limit.py` 60/min; `app/core/logging.py` PII-safe, truncation; `app/main.py` CORS disabled by default, rate limit middleware, `app/core/errors.py` safe error handling; `THREAT_MODEL.md` + `OPERATIONAL_CHECKLIST.md`; 13 tests highest-risk (injection, sanitization, SSRF, rate limit, PII, gate not weakened)
- Phase 13: implemented — evaluation suite (`tests/evaluation_dataset.json` 16 scenarios, `tests/test_evaluation.py` 13 tests: unit, integration, mocked Gmail/LLM, approval-gate, injection, hallucination/unsupported claim, duplicate, failure/retry, knowledge retrieval, e2e; `EVALUATION.md` with accuracy 87.5% mock, limitations documented, no perfection claim)
- Phase 14: implemented — production config (`app/core/config.py` phase `14_production`, `.env.production.example`, `Dockerfile` gunicorn 2 workers, `docker-compose.yml` Postgres, `gunicorn_conf.py`, `nginx.conf.example` HTTPS, `app/api/health.py` /health liveness + /ready readiness, CORS disabled by default, rate limit 60/min, `DEPLOYMENT.md` (env, secrets, DB Postgres, worker, HTTPS, health/readiness, logging, backup, OAuth checklist, quota), local dev still works)
- Phase 15: final — verification of all integrations, 149 tests pass, docs `README.md` (arch, setup, env, OAuth, run commands, API 52 paths, workflow diagram, security, troubleshooting, production checklist, limitations), release checklist complete
- All 16 phases (0–15) integrated; no scaffold remaining

## Local Run
```
pip install -r requirements.txt
cp .env.example .env  # fill placeholders
uvicorn app.main:app --reload --port 8000
# health: http://localhost:8000/health
# docs:  http://localhost:8000/docs
pytest -v
```

## Files Changed (Phase 0+1)
- app/core/config.py, logging.py, errors.py
- app/api/health.py
- app/main.py (cleaned, phase 1 foundation)
- ARCHITECTURE.md
- .env.example, requirements.txt
- tests/*
