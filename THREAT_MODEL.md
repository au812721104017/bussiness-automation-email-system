# Threat Model — AI Email Auto-Reply Agent (Phase 12)

## Assets
- Gmail account, OAuth tokens, email bodies (PII), draft replies, approval decisions, audit logs, business knowledge.

## Trust Boundaries
- Untrusted: incoming email content (sender-controlled), HTML/attachments, links, Gmail API responses.
- Trusted: approved `knowledge/` docs, configuration via env, admin actions with `ADMIN_TOKEN`, DB.

## Threats & Mitigations

| Threat | Impact | Mitigation (Phase) |
|--------|--------|---------------------|
| **Prompt injection in email** | Classifier/reply follows injected instructions, leaks prompt, changes tone, bypasses policy | System instructions wrap email as `UNTRUSTED DATA` with `BEGIN/END` markers; injection detection (`ignore previous instructions`, `you are now`, `system:`) flags `injection_detected` and caps confidence; validation coerces unknown→low confidence; tests `test_prompt_injection_treated_as_untrusted` |
| **Hallucination / invented policies/prices** | Sends false promises | Reply generator `REPLY_SYSTEM` says never invent; if missing info → `needs_review` true, low confidence; `guarantee`/`discount` phrases auto-flag `needs_review`; RAG citations required from `knowledge/`; tests `test_nonexistent_invented_policy_flagged` |
| **Secret / token / PII logging** | Credential leak | Tokens never logged (`has_token` boolean only in `gmail_service.py:154`, `gmail_send_service.py`); `StructuredFormatter` redacts `***@domain`, `sk-***`, `ya29.***` and truncates long messages/extra_data; audit stores `safe_metadata` only (snippet not full body) |
| **Gmail send without approval** | Unattended spam | Critical gate `can_send(db, run_id, draft_id)` checks `status==approved` + exact `run_id`/`draft_id` + `draft.updated_at <= approval.decided_at`; `approval_required` defaults true; changing draft auto-invalidates (`PUT /drafts/{id}` → `invalidate_on_draft_change`); every send path calls gate; tests `send_without_approval_blocked`, `modified_draft_blocked`, `no_hidden_bypass` |
| **HTML injection / XSS via email body** | Stored XSS in draft/admin | `sanitize_html` in `app/core/security.py` strips `<script><iframe><object>` etc., `html.unescape` + tag removal, used in `GmailService.normalize_message` HTML fallback; admin dashboard escapes via `textContent` not `innerHTML` for user data (JS uses template literals but should escape); tests `test_html_sanitization` |
| **Malicious attachments / links** | Malware, phishing | `has_malicious_attachment` blocks `.exe/.bat/.js` etc.; `extract_safe_links` filters `javascript:`/`data:`; RAG never fetches URLs from email (SSRF risk); SSRF check `is_ssrf_url` blocks `localhost`, `169.254`, `10/172.16/192.168`, `metadata.google.internal`; no outbound fetch from email content |
| **SSRF via email links** | Internal network probe | `is_ssrf_url` in `app/core/security.py` blocks private nets; no code path fetches email-provided URLs; only `knowledge/` dir is ingested |
| **OAuth scope escalation** | Over-privileged Gmail | Phase 3: `gmail.readonly`; Phase 8: `gmail.send` separate (`gmail_send_scopes`); minimal scopes documented in `GMAIL_SETUP.md`; `gmail_scopes` configurable |
| **Auth bypass on admin / approvals** | Unauthorized approve/send | `ADMIN_TOKEN` env; `_admin_auth` dependency checks `X-Admin-Token`; dev allows without token with warning, production requires; mutation endpoints (`/admin/drafts/{id}/approve`, `/admin/emails/{id}/ignore`, `/scheduler/start`) behind auth; rate limiting per IP/path |
| **Rate limit / abuse** | Brute force, Gmail quota | `RateLimiter` `60/min per IP/path` via middleware (`app/core/rate_limit.py`); excluded health/docs; returns `429`; Gmail quota considerations in prod checklist |
| **CORS misconfiguration** | Cross-origin abuse | CORS disabled by default (`cors_allow_origins=""`); only enabled if `CORS_ALLOW_ORIGINS` set to explicit origins |
| **DB transaction / idempotency** | Duplicate sends, lost audits | Unique `gmail_message_id`, `run_id`; `orchestrator` checks `EmailMessage` existence before create; `poller` in-memory + DB dedup; `SessionLocal` transactions with commit/refresh; `Base.metadata.create_all` for dev, Alembic-ready for prod |
| **Retry amplification** | Infinite loop on permanent error | Poller distinguishes transient (`timeout`, `unavailable`, `rate`, `503`) → bounded retries (`scheduler_max_retries=3`, backoff), permanent (`validation`, `permission`) → no retry, add to processed cache to avoid re-poll |
| **Dependency vulnerabilities** | Supply chain | Pinned `requirements.txt`; `pip audit` / `safety` in operational checklist; no dynamic code exec |
| **Approval bypass via direct function / background job** | Hidden send | `can_send` is single gate used by `GmailSendService` and `AgentOrchestrator.send_if_approved`; scheduler stops at `pending_approval` (`app/jobs/poller.py` never calls send); grep ensures no `if True: return True` bypass |

## Remaining Risks (documented, not hidden)
- Mock LLM in dev may misclassify; prod should use evaluated provider with human review for `risk_level=high`.
- DNS rebinding SSRF not fully mitigated (only literal IP check); prod should use egress proxy.
- Rate limiter is in-memory, not distributed; scale to Redis in prod.

## Verification (Phase 12)
- Tests `test_security.py` cover injection, HTML sanitization, malicious attachment, SSRF, PII redaction, CORS defaults, rate limit, gate not weakened.
- Audit completeness: every state transition creates `AuditEvent` with `timestamp, run_id, event_type, status, safe_metadata` (checked in `test_audit_event_creation_contains_required_fields`).
