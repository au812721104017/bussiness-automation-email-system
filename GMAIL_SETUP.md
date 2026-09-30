# Gmail OAuth Setup — Phase 3 (Read-Only)

## Scopes (minimum for Phase 3)
- `https://www.googleapis.com/auth/gmail.readonly` — read messages, threads, headers, body. **Do not request `gmail.send` or `gmail.modify` in this phase.**

## Local Setup (read-only first)
1. **Google Cloud Console** → https://console.cloud.google.com/
2. Create project → Enable **Gmail API** (APIs & Services → Library)
3. **OAuth consent screen** → External (testing) → Add test user = your Gmail
4. **Credentials** → Create Credentials → OAuth client ID → Web application
   - Authorized redirect URI: `http://localhost:8000/gmail/auth/callback` (or `GMAIL_REDIRECT_URI` in `.env`)
5. Download `credentials.json` → place at path `GMAIL_CREDENTIALS_PATH` (default `./credentials.json`)
6. In `.env` set:
   ```
   GMAIL_CLIENT_ID=your-id.apps.googleusercontent.com
   GMAIL_CLIENT_SECRET=your-secret
   GMAIL_REDIRECT_URI=http://localhost:8000/gmail/auth/callback
   GMAIL_SCOPES=https://www.googleapis.com/auth/gmail.readonly
   GMAIL_CREDENTIALS_PATH=./credentials.json
   GMAIL_TOKEN_PATH=./token.json
   ```
7. Start app: `uvicorn app.main:app --reload`
8. Open `http://localhost:8000/gmail/auth/url` → visit `auth_url` → consent → Google redirects with `code`
9. Exchange code (via `google-auth-oauthlib` Flow) — token saved to `GMAIL_TOKEN_PATH` automatically by the flow helper in `app/services/gmail_service.py:58`. For Phase 3 you may use the helper script or manual flow:
   ```python
   from google_auth_oauthlib.flow import Flow
   flow = Flow.from_client_config({...}, scopes=["https://www.googleapis.com/auth/gmail.readonly"])
   flow.redirect_uri = "http://localhost:8000/gmail/auth/callback"
   auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
   # visit auth_url, get code, then:
   flow.fetch_token(code=code)
   creds = flow.credentials
   with open("token.json","w") as f: f.write(creds.to_json())
   ```
10. Verify: `GET /gmail/auth/status` → `authenticated:true`, `GET /gmail/messages` should list.

## Security
- **Never commit** `credentials.json` or `token.json` — they are gitignored. Tokens are never logged (see `app/services/gmail_service.py:85` redaction).
- Request minimum scopes; document any scope widening in Phase 8 (send).
- Never log OAuth tokens, full email bodies, or PII raw. Logs contain only `message_id`, `thread_id`, `body_len`, `sender_domain` (`gmail_service.py:142`).
- In tests, Gmail client is injected mock — no network, no token file needed (`tests/test_gmail.py`).

## Verification (Phase 3)
- App starts without Gmail configured: `GET /gmail/auth/url` returns `configured:false` with setup hint, health still 200.
- With mocked client: list, fetch, thread, normalize all work; malformed/empty inputs raise `ValueError` safely.

## Production Note
- For production, store tokens securely (e.g., encrypted DB, Secret Manager), use HTTPS redirect URI, rotate secrets. Enable only when approval gate (Phase 7) is verified.
