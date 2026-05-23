# IG CRM — Improvements Review

> Date: 2026-05-22. Full-project pass: what was improved this session, and a
> prioritized backlog of what to add/fix next. Companion to `SECURITY_AUDIT.md`.

## Done this session (autonomous)
- 🔒 **Secret encryption** (cookies + ig_password, Fernet) + removed secrets from API responses.
- 🛡️ **Auth + multi-tenancy**: JWT guard + per-user scoping on accounts/tasks/orchestrator; proxy/media/ai require login; `/users`+`/admin` admin-only.
- 🧑‍✈️ **Agents + capacity**: `agents_limit` per subscription, worker self-retry gate on agent slots and host CPU/RAM/browser ceiling (psutil), live `/admin/capacity`.
- 🧩 **Proxy moved into the account card** (create/edit per account); standalone Proxies tab removed.
- ✅ **Tests green** (56 passed): fixed the suite for the new auth, added `test_security.py` (encryption, agents, capacity, admin, ownership-from-token, no-secret-in-read).
- 🐛 Fixed stale `clarification` assertion; runbooks now use `python -m` (broken venv shebangs); `pnpm.onlyBuiltDependencies` for sharp.

## Backend backlog (priority)
1. **Rate-limit `/auth/*`** (slowapi + Redis) — see audit.
2. **Disable `/docs`/`/openapi.json` in prod**; add an `ENV` setting.
3. **Audit log** table for admin actions (who granted what, when).
4. **`started_at`/`completed_at` on Task** — currently only `created_at`; the 24h dashboard counts and reaper use `created_at` as a proxy. Add real timestamps for accurate metrics + retry history.
5. **GIN index on `instagram_accounts.tags`** (fan-out tag query) — already noted in ARCHITECTURE; add the migration.
6. **`validate_account_session` is a stub** (always returns valid) — wire a real cookie check via TaskExecutor.
7. **`obfuscate_link` is a placeholder** (spintax) — wire a real redirect service before live link traffic.
8. **Consistent error envelope** + request IDs in logs for easier debugging during the test.

## Frontend backlog (priority)
1. **Token refresh / expiry UX** — JWT lasts 24h; on 401 the client redirects to /login (ok), but add a "session expired" toast.
2. **Orchestrator "Sims taskbar"** polish — drag-reorder steps, duplicate step, save plan as template.
3. **Dashboard chart** currently uses the first account with follower data; aggregate across the fleet once `account_metrics` fills in.
4. **Optimistic UI + error toasts** are partial; standardize across pages.
5. **Empty/loading skeletons** consistent everywhere (accounts/tasks have them; media/activity could use them).
6. **Accessibility**: dialogs/labels are mostly fine via Radix; audit focus traps + keyboard nav before launch.

## Product / ops
1. **Real-IG dry run checklist** (proxies valid, cookies fresh, 1 account, watch `/activity`).
2. **Seed/demo data** script (admin + a couple of accounts + fake metrics) so the dashboard isn't empty in demos.
3. **WebSocket/SSE for `/activity`** instead of 2s polling — true live step progress from the worker.
4. **Backups** of the Postgres volume before the live test (cheap insurance).
5. **One-command dev** (`Makefile` / `justfile`) to start db+redis+api+worker+beat+front.

## Known gotchas (carried from this session)
- `.venv/bin/{alembic,uvicorn,celery}` have stale shebangs → run via `../.venv/bin/python -m <module>`.
- `pnpm` not on PATH → `corepack pnpm` for install, `./node_modules/.bin/next dev` for dev.
- LLM key was shared in chat → rotate it.
