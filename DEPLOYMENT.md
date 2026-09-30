# Deployment — AI Email Auto-Reply Agent (Phase 14)

## Not Deployed — Preparation Only
This phase prepares for production without actually deploying. Follow this guide when you are ready.

## 1. Production Environment Configuration
- Copy `.env.production.example` to `.env` on the server (never commit `.env`)
- Required: `ENV=production`, `DATABASE_URL=postgresql://...`, `SECRET_KEY` (64 random), `ADMIN_TOKEN` (32+ random), `OPENAI_API_KEY`, `GMAIL_CLIENT_ID/SECRET`, `GMAIL_REDIRECT_URI=https://yourdomain.com/gmail/auth/callback`
- Optional: `LLM_PROVIDER`, `RAG_*`, `SCHEDULER_ENABLED` (keep false until gate verified), `RATE_LIMIT_PER_MINUTE`, `CORS_ALLOW_ORIGINS`
- Validate: `python -c "from app.core.config import get_settings; print(get_settings().phase)"` should be `14_production`

## 2. Secure Secret Management Guidance
- **Never hard-code** secrets; use env vars or a Secret Manager (AWS Secrets Manager, GCP Secret Manager, Vault)
- Mount `credentials.json` and `token.json` at `/app/secrets/` with `600` permissions, owned by `appuser`
- `.gitignore` already covers `.env`, `credentials.json`, `token.json`, `*.db`
- Rotate secrets if leaked: regenerate Google OAuth client, re-run OAuth flow

## 3. Database — PostgreSQL (Production) vs SQLite (Local)
- Local dev: `DATABASE_URL=sqlite:///./email_automation.db` (already works)
- Production: `DATABASE_URL=postgresql://user:password@db:5432/email_agent_prod`
- SQLAlchemy `database_url` validator allows both (`app/core/config.py`); `connect_args check_same_thread` only for sqlite
- For production, run `Base.metadata.create_all` on first boot (dev) or use Alembic:
  ```
  pip install alembic
  alembic init alembic
  # configure alembic/env.py to import app.db.base.Base
  alembic revision --autogenerate -m "init"
  alembic upgrade head
  ```
- Postgres-ready: `ThreadState`, `AuditEvent` use `DateTime(timezone=True)` and text JSON for portability

## 4. Worker / Process Model
- Dockerfile uses `gunicorn` with `uvicorn.workers.UvicornWorker` (`-w 2`) — adjust workers = `2*CPU+1`
- Local dev: `uvicorn app.main:app --reload --port 8000` (single worker, auto-reload)
- Behind reverse proxy (Nginx/Caddy), run `gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120`
- Health lives in `app/main.py` lifespan; scheduler thread is daemon and stops gracefully on SIGTERM

## 5. HTTPS / Reverse Proxy Guidance
- Terminate TLS at Nginx/Caddy/ALB; do not expose `uvicorn` directly
- Example Nginx:
  ```nginx
  server {
    listen 443 ssl;
    server_name yourdomain.com;
    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    location / {
      proxy_pass http://127.0.0.1:8000;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    }
  }
  ```
- Set `GMAIL_REDIRECT_URI` to `https://yourdomain.com/gmail/auth/callback` in Google Cloud console
- Redirect `http` → `https`

## 6. Health / Readiness Checks
- **Liveness:** `GET /health` → `{"status":"healthy","phase":"14_production",...}` (used by Docker `HEALTHCHECK` and K8s `livenessProbe`)
- **Readiness:** same `/health` plus DB check (app fails fast if `DATABASE_URL` invalid); for K8s:
  ```yaml
  livenessProbe: { httpGet: {path: /health, port: 8000}, periodSeconds: 30 }
  readinessProbe: { httpGet: {path: /health, port: 8000}, periodSeconds: 10 }
  ```
- **Docs:** `/docs` (OpenAPI) — disable in prod via `FastAPI(docs_url=None)` if needed, or protect with `ADMIN_TOKEN`

## 7. Logging / Monitoring Hooks
- Structured JSON logging via `app/core/logging.py` (`StructuredFormatter`); redacts PII
- Levels via `LOG_LEVEL`; set `INFO` in prod
- Hook to Datadog/CloudWatch: ship stdout JSON; filter `level=ERROR` for alerts
- Audit logs: `audit_events` table with `run_id` traceability; query via `GET /admin/audits?run_id=...` or `GET /scheduler/polls`
- Metrics to watch: `SCHEDULER_POLL_FAILED`, `SEND_FAILED`, `APPROVAL_REJECTED` rate, `rate_limited` 429 count

## 8. Backup Strategy
- **DB:** Nightly `pg_dump` via cron:
  ```
  0 2 * * * pg_dump postgresql://user:pass@db/email_agent_prod | gzip > /backups/email_agent_$(date +\%F).sql.gz
  ```
  Keep 7 days, encrypt with `gpg`, test restore monthly
- **Knowledge:** `knowledge/` is git-tracked; backup via repo
- **Secrets:** backup encrypted outside repo

## 9. OAuth Production Setup Checklist
- [ ] Google Cloud: Enable Gmail API
- [ ] OAuth consent screen: Production (not Testing), add `yourdomain.com`
- [ ] Credentials: OAuth client ID (Web) with `https://yourdomain.com/gmail/auth/callback`
- [ ] Scopes: `gmail.readonly` (Phase 3), `gmail.send` (Phase 8) — request minimum, no `gmail.modify`
- [ ] Complete flow: `GET /gmail/auth/url` → consent → `token.json` saved at `GMAIL_TOKEN_PATH`
- [ ] Verify `GET /gmail/auth/status` returns `authenticated:true`

## 10. Gmail API Quota / Error Considerations
- Quota: ~250 quota units/sec; `users.messages.list` costs 5, `send` costs 100. Poll interval `300s` (prod default) avoids quota; `SCHEDULER_QUERY=in:inbox -in:sent -in:spam -in:trash newer_than:{window}d` (`SCHEDULER_NEWER_THAN_DAYS`, default 2) with `maxResults=10` — read-state agnostic, so READ and UNREAD inbound mail are both polled
- Errors: `429` rate limiting is handled via transient retry in `app/jobs/poller.py` (bounded `SCHEDULER_MAX_RETRIES=3`); permanent `validation` not retried
- On `401` (token expired): re-auth via `GET /gmail/auth/url` flow

## 11. Keep Local Dev Working
- `DATABASE_URL` defaults to `sqlite` if `POSTGRES` not set; `ENV=development` allows `ADMIN_TOKEN` empty with warning
- Run locally: `pip install -r requirements.txt && cp .env.example .env && uvicorn app.main:app --reload`
- `pytest -q` runs with `ENV=testing` and scheduler disabled

## 12. Approval Gate — Do Not Weaken
- `APPROVAL_REQUIRED=true` by default; production unattended send must be explicit (`APPROVAL_REQUIRED=false` is risky and logs warning)
- All sends call `can_send(db, run_id, draft_id)` (`app/services/approval_service.py`); draft change invalidates
- Scheduler stops at `pending_approval` — never auto-sends

## Commands
```
# local
cp .env.example .env
uvicorn app.main:app --reload
pytest -v

# prod (Docker)
docker compose up -d db
docker compose build app && docker compose up -d app
docker compose logs -f app
curl https://yourdomain.com/health
```

See also: `THREAT_MODEL.md`, `OPERATIONAL_CHECKLIST.md`, `EVALUATION.md`, `GMAIL_SETUP.md`.
