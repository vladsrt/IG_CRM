# IG CRM — Security Audit (authorized pentest)

> Date: 2026-05-22. Authorized by the owner, run against the locally-running
> stack (FastAPI :8000 + Postgres :5433). Black/grey-box + code review.
> Scope: API auth, multi-tenancy, injection, secret handling, info disclosure.

## TL;DR
The auth/JWT and DB layers are solid. The one real, high-severity issue —
**account secrets returned by the API and stored in plaintext** — has been
**fixed** (Fernet encryption at rest + secrets removed from responses).
Remaining items are hardening (rate-limit, docs exposure, CORS) — none block a
controlled pilot.

---

## Findings

### 🔴 HIGH — Account secrets exposed (FIXED)
- **What:** `GET/POST /accounts` returned `ig_password` and `cookies` in the
  JSON body, and they were stored plaintext in Postgres. Confirmed live: a
  created account echoed `TOPSECRETPW` and the session cookie value.
- **Impact:** Anyone with one API response or a DB dump harvests live Instagram
  sessions for the whole fleet.
- **Fix (done):**
  - `app/core/secrets.py` — Fernet encryption (key derived from `SECRET_KEY`).
  - `crud/account.py` encrypts `ig_password` + `cookies` on write.
  - `schemas/account.py` — `InstagramAccountRead` no longer contains
    `ig_password`/`cookies` (split public vs secret base).
  - Worker decrypts only at browser-launch time (`celery_tasks.py`).
  - Verified: API no longer leaks; DB stores `enc::…`; worker decrypts OK.
- **Residual:** key is derived from `SECRET_KEY`; rotating it invalidates stored
  secrets (re-enter them). Decrypt fails open to avoid crashing a run.

### 🟠 MEDIUM — Privilege escalation via self-service subscription (FIXED)
- **What:** `PATCH /users/{id}/subscription` had no admin check — any logged-in
  user could upgrade themselves to enterprise / change agent limits.
- **Fix (done):** the whole `/users` router now requires admin; tier/agent
  changes go through the admin-only `/admin/users/{id}`. Verified 403 for
  non-admins.

### 🟡 LOW — No rate limiting on `/auth/login` (OPEN)
- Brute-force / credential-stuffing possible.
- **Plan:** add `slowapi` (or a Redis token bucket) — e.g. 10 login attempts /
  min / IP. ~30 min of work. Not a blocker for a private pilot.

### 🟡 LOW — API docs + OpenAPI publicly served (OPEN)
- `/docs` and `/openapi.json` return 200 with no auth → endpoint enumeration.
- **Plan:** gate behind admin or disable in production
  (`FastAPI(docs_url=None, openapi_url=None)` when `ENV=prod`).

### 🟡 LOW — CORS allow-all + permissive (ACCEPTED for test)
- `CORS_ALLOWED_ORIGINS=["*"]`. Fine for the tunnel test; tighten to the Vercel
  domain before any public launch.

### ℹ️ INFO — Frontend XSS surface
- React auto-escapes, so reflected XSS is unlikely. Watch any future
  `dangerouslySetInnerHTML`. `Asset.metadata_['original_filename']` should be
  sanitized at the upload boundary if ever rendered raw (it isn't today).

---

## Tested and found SAFE
| Vector | Result |
|---|---|
| SQL injection (login user/pass, JSON fields, query params) | Safe — SQLAlchemy parameterizes; payloads stored literally, tables intact |
| JWT: no token / garbage / `alg=none` / tampered signature | All rejected 401 |
| JWT: weak-secret forgery (common secrets) | No common secret works (strong random key) |
| IDOR: user B reading/dispatching user A's accounts/tasks | Blocked (404 / scoped out) |
| Mass-assignment of `user_id` on account create | Ignored — owner taken from token |
| Oversized AI prompt (50k chars) | 422 (max_length guard) |
| Path traversal in worker file ops | Defended by `resolve_within_media_root` (symlink-aware) |

---

## Recommended next hardening (priority order)
1. Rate-limit `/auth/*` (slowapi + Redis).
2. Disable `/docs` & `/openapi.json` in production.
3. Tighten CORS to the real frontend origin.
4. Add an audit log for admin actions (tier/agent grants).
5. Rotate the LLM API key that was shared in chat.
6. Consider per-user proxy scoping (proxies are currently a shared pool with a
   FK from the account; not exposed cross-user via the UI after the proxy-tab
   removal, but the `/proxies` API is still global to any logged-in user).
