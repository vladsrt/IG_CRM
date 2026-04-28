# Instagram CRM — Architecture

> Living technical reference for the IG CRM backend.
> Read this before adding a new feature, writing a frontend integration,
> or onboarding another agent.

---

## 1. System Overview

Instagram CRM is a multi-tenant SaaS platform that lets an operator drive a
fleet of Instagram accounts through a single conversational interface. The
operator types natural-language instructions ("warm up my crypto accounts,
then post the new reel everywhere by tonight"); the system plans the work
with an LLM, the operator reviews and approves, and a fleet of headless
Chromium workers executes the plan against real Instagram sessions through
authenticated proxies.

The platform is built around three layers:

| Layer | Responsibility | Tech |
|---|---|---|
| **API / HTTP** | Auth, CRUD, plan review, dispatch | FastAPI, SQLAlchemy 2.0 (sync), PostgreSQL |
| **AI brain** | Translate prompts into strict, machine-readable plans | OpenAI Structured Outputs (`client.beta.chat.completions.parse`), Pydantic v2 |
| **Worker fleet** | Execute plans against real Instagram | Celery (Redis broker), DrissionPage (Chromium), FFmpeg |

Billing is **subscription-based** (Free / Pro / Enterprise) — there is no
token-balance model. A `User` row owns exactly one `Subscription` row,
auto-provisioned at signup with `tier="free"`.

### High-level request flow

```
                                     ┌──────────────────┐
        User prompt                   │  Frontend (UI)   │
        ──────────────────────────►   │  (chat + review) │
                                     └────────┬─────────┘
                                              │ 1. POST /ai/generate-task
                                              ▼
                                     ┌──────────────────┐
                                     │  AIParser        │  ◄── OpenAI
                                     │  (services/)     │
                                     └────────┬─────────┘
                                              │ 2. ParsedTaskPlan (JSON)
                                              ▼
                          ┌─── clarification_needed? ───┐
                          │                             │
                          │ yes → ask user              │ no → operator approves
                          │                             ▼
                                     ┌──────────────────────┐
                                     │  POST /orchestrator/  │
                                     │  tasks/fan-out        │
                                     └────────┬──────────────┘
                                              │ 3. one Task row per
                                              │    matching account
                                              ▼
                                     ┌──────────────────────┐
                                     │  Celery + Redis       │
                                     └────────┬──────────────┘
                                              │ 4. run_instagram_task(task_id)
                                              ▼
                                     ┌──────────────────────┐
                                     │  TaskExecutor         │
                                     │  + InstagramBrowser   │
                                     │  + DrissionPage       │ ◄── Proxy
                                     └──────────────────────┘
```

---

## 2. Data Models

All ORM models live under [`backend/app/models/`](backend/app/models/) and
share a common SQLAlchemy 2.0 `Base`. UUIDs are used everywhere as primary
keys.

### `User` — [`models/user.py`](backend/app/models/user.py)
The platform tenant. One-to-many with `InstagramAccount` and `Asset`,
**one-to-one** with `Subscription`. `cascade="all, delete-orphan"` on every
child relationship — deleting a user wipes their entire tenant footprint.

### `Subscription` — [`models/billing.py`](backend/app/models/billing.py)
Single row per user. Columns: `tier` (`free` / `pro` / `enterprise`),
`valid_until`, `is_active`. Auto-created in
[`crud/user.py::create_user()`](backend/app/crud/user.py) so a user always
has a billing row.

### `InstagramAccount` — [`models/account.py`](backend/app/models/account.py)
Belongs to a user, optionally bound to a `Proxy`. Stores `ig_username`,
`ig_password`, `cookies` (JSONB — list-of-dicts in IG export format), and
`auth_method` (`cookies` | `manual`).

The **`tags: JSONB`** column is the targeting primitive. It holds a
lowercase array (e.g. `["crypto", "tier1"]`) and is queried via JSONB
containment (`tags @> '["crypto"]'::jsonb`). Tags are normalized
(lowercased, stripped, deduped) at the CRUD boundary.

### `Proxy` — [`models/proxy.py`](backend/app/models/proxy.py)
Authenticated HTTP proxy. Fields: `host`, `port`, `username`, `password`,
`rotation_url`, `type` (`ordinary` | `sticky`). Rendered into the format
the worker expects (`user:pass@host:port`, no scheme prefix) by
[`celery_tasks._build_proxy_string()`](backend/app/workers/celery_tasks.py).

### `Task` — [`models/task.py`](backend/app/models/task.py)
The unit of execution. Belongs to one `InstagramAccount`. Holds:
- `payload: JSONB` — the full `ParsedTaskPlan` dict (summary, priority,
  target_tags, commands)
- `status: str` — finite state machine, see below
- `priority: int` — derived from `TaskPriority` enum (low=0, normal=5,
  high=10, urgent=20)
- `error_log: text` — populated on failure with the worker's traceback

**Lifecycle:**
```
DRAFT ──► PENDING ──► RUNNING ──► COMPLETED
   ▲          │            │
   │          │            └──► FAILED
   └──────────┘  (broker-dispatch failure rolls back)
```

### `MediaFolder` — [`models/asset.py`](backend/app/models/asset.py)
User-owned bucket for grouping related assets. Created via
`POST /media/folders`; deleted via cascade with the owning user.

### `Asset` — [`models/asset.py`](backend/app/models/asset.py)
A media file on disk. Forms a **parent → children tree**: a freshly
uploaded video is the parent (raw original); each FFmpeg-uniqueized clone
is a child linked through `parent_id`. Important fields:
- `file_path: str` — absolute path under `settings.MEDIA_ROOT`
- `folder_id: UUID | None` — optional `MediaFolder` membership
- `parent_id: UUID | None` — self-FK; null on raw originals
- `status: str` — FSM (`raw` | `processing` | `ready`)
- `metadata_: JSONB` — ffmpeg fingerprint + settings on variants

**Asset lifecycle:**
```
upload                    uniqueize start          uniqueize done
──────► RAW ──────────────► PROCESSING ──────────► READY
                                  │
                                  └─ on failure ──► RAW
```

---

## 3. The AI Flow (Agentic Workflow)

Endpoint: **`POST /ai/generate-task`** —
[`api/routers/ai.py`](backend/app/api/routers/ai.py)

This endpoint is **side-effect free**. It does not write to the database
and does not enqueue any work. Its only job is to convert a natural-language
prompt into a strict `ParsedTaskPlan` and return it to the operator for
review (Human-in-the-Loop).

### Request

```json
POST /ai/generate-task
{
  "user_prompt": "post the new reel to all my crypto accounts tonight",
  "account_id": null
}
```

`account_id` is optional context — kept for backward compatibility, not
used by the parser.

### Implementation

1. The router calls
   [`AIParser.parse(user_prompt)`](backend/app/services/ai_parser.py).
2. The parser invokes `client.beta.chat.completions.parse(..., response_format=ParsedTaskPlan)`
   so OpenAI returns a strictly-validated Pydantic instance (no JSON
   parsing in our code path).
3. The router returns the plan as-is with `200 OK`.

### `ParsedTaskPlan` schema

```python
class ParsedTaskPlan(BaseModel):
    summary: str
    priority: TaskPriority           # low | normal | high | urgent
    target_tags: list[str]           # lowercase, single-word
    clarification_needed: str | None # nullable; question or null
    commands: list[ActionCommand]    # list of {action, args}
```

Each `ActionCommand` carries `action` (a value from a closed enum:
`upload_reels`, `upload_post`, `warmup`, `send_dm`, `like_post`, …) and
`args: list[ActionArgument]` where each argument is a typed key/value
pair. The list-of-pairs shape is mandatory: OpenAI strict mode forbids
open `dict[str, Any]` (no `additionalProperties: true`). The helper
`ActionCommand.args_as_dict()` re-materializes a regular dict downstream
and JSON-decodes any non-string values.

### Agentic clarification (`clarification_needed`)

`clarification_needed` is **nullable, not optional**. OpenAI strict mode
requires every property to be in `required`, so the field is modeled as
`str | None`. The model MUST emit a value but is *instructed by the system
prompt* to emit `null` whenever no question is needed.

The system prompt
([`services/ai_parser.py::SYSTEM_PROMPT`](backend/app/services/ai_parser.py))
contains an explicit rule:

> If the request is ambiguous OR is missing information, set
> `clarification_needed` to ONE polite, specific question ending in `?`,
> set `commands` and `target_tags` to empty lists, and still write a
> `summary` of what was understood.

Concrete triggers:
- Upload requested with no `file_path` / asset reference
- "Post to my accounts" with no tag, count, or specific accounts
- DM / comment / follow with no recipient or text
- Any required argument that cannot be inferred

### Frontend chat loop

```
operator types prompt
        │
        ▼
POST /ai/generate-task ──► ParsedTaskPlan
                              │
        ┌─────────────────────┴─────────────────────┐
        │ clarification_needed != null              │
        ▼                                           ▼
  show question as                          show plan summary +
  assistant reply                           commands; user clicks
        │                                   "Approve & dispatch"
        ▼                                           │
operator replies, frontend                          ▼
re-sends prompt + reply                  POST /orchestrator/tasks/fan-out
```

---

## 4. The Fan-Out Orchestrator

Endpoint: **`POST /orchestrator/tasks/fan-out`** —
[`api/routers/orchestrator.py`](backend/app/api/routers/orchestrator.py)

Takes an operator-approved `ParsedTaskPlan` and dispatches one Celery job
per matching Instagram account.

### Request

```json
POST /orchestrator/tasks/fan-out
{
  "plan": { ... ParsedTaskPlan ... },
  "target_account_ids": ["uuid-a", "uuid-b"]
}
```

### Pre-flight

The endpoint refuses to dispatch:
- `400` if `plan.clarification_needed != null` — the operator skipped the
  chat step
- `400` if `plan.commands == []` — the model produced no actions

### Account resolution

Implemented in `_resolve_target_accounts()`. Returns a `dict[UUID, Account]`
plus a `list[UUID]` of explicit IDs that did not exist:

1. If `plan.target_tags` is non-empty, query
   [`crud_account.list_accounts_by_tags()`](backend/app/crud/account.py),
   which uses JSONB containment.
2. Then iterate `target_account_ids`. For each, if not already resolved
   and the row exists, add it; otherwise record it as missing.

If the union is empty → `400`. Otherwise the missing explicit IDs are
**reported** in `skipped[]` rather than 400'd, so the operator gets
partial-success semantics.

### Per-account dispatch

For each resolved account, `_create_and_dispatch_one()` performs:

1. `crud_task.create_task()` with `status=PENDING`,
   `payload=plan.to_payload_dict()`, `priority=plan.priority_as_int()`.
2. `run_instagram_task.delay(str(task.id))` — hands to Celery.

If step 1 fails, the account goes to `skipped[]`. If step 2 fails (broker
unreachable), the just-created Task is rolled back to `DRAFT` (so the
operator can retry it via the single-task endpoint) and the account also
goes to `skipped[]`.

### Response

```json
{
  "requested_accounts": 12,
  "dispatched_count": 11,
  "skipped_count": 1,
  "dispatched": [
    {"task_id": "...", "account_id": "...", "celery_task_id": "..."}
  ],
  "skipped": [
    {"account_id": "...", "reason": "celery dispatch failed: ..."}
  ],
  "celery_task_ids": ["..."]
}
```

A single account never aborts the entire fan-out — failures are isolated
per row.

---

## 5. The Worker Node (DrissionPage)

Celery task: **`run_instagram_task(task_id)`** —
[`app/workers/celery_tasks.py`](backend/app/workers/celery_tasks.py)

The worker process is what actually drives Chromium. The Celery task is a
thin wrapper around the **`TaskExecutor`** —
[`workers/core/executor.py`](backend/workers/core/executor.py).

### Pipeline inside `run_instagram_task`

1. Open a fresh `SessionLocal()` (worker processes don't share request
   sessions).
2. Load `Task`, `InstagramAccount`, `Proxy`. Build the executor payload:
   ```python
   payload = {
       "task_id":          str(task.id),
       "account_id":       str(account.id),
       "ig_username":      account.ig_username,
       "ig_password":      account.ig_password,
       "auth_method":      account.auth_method,
       "proxy_string":     "user:pass@host:port",   # no scheme
       "proxy_session_id": account.proxy_session_id,
       "cookies":          account.cookies or {},
       "commands":         plan["commands"],          # list, flattened
       "plan_summary":     plan["summary"],
       "plan_priority":    plan["priority"],
       "priority":         task.priority,
   }
   ```
3. Flip `Task.status` → `RUNNING`.
4. Call `TaskExecutor(payload).execute()`.
5. On success → `COMPLETED`; on `SoftTimeLimitExceeded` or any other
   exception → `FAILED` with the traceback persisted to `Task.error_log`.

### `TaskExecutor.execute()`

```python
def execute(self) -> dict:
    browser = None
    try:
        browser = InstagramBrowser(
            proxy_string=self.proxy_string,
            user_agent=self.user_agent,
            headless=self.headless,
            task_id=self.task_id,           # unique plugin folder
        )
        if self.cookies:
            browser.inject_cookies(self.cookies)

        for command in self.commands:
            handler = ACTION_REGISTRY[command["action"]]
            handler(browser, command["args"])

    finally:
        # Story 4.4 — guaranteed teardown.
        if browser is not None:
            try:
                browser.close()
            except Exception:
                logger.exception("teardown raised; continuing")
```

### Story 4.4 — Guaranteed Garbage Collection

The `try/finally` is load-bearing. Even if a handler crashes, even if
`browser.close()` itself raises, the process always returns to the Celery
wrapper, which marks the Task `FAILED`. The browser is always reaped, the
proxy-plugin folder is always deleted.

### Story 4.5 — Per-task proxy isolation

The historical `InstagramBrowser` hard-coded a single `runtime_proxy_plugin`
folder name, so two concurrent tasks in the same worker would collide on
disk. Now —
[`workers/core/browser_core.py`](backend/workers/core/browser_core.py):

```python
self.plugin_folder = f"runtime_proxy_plugin_{sanitize(task_id) or uuid4().hex}"
```

`_sanitize_token()` strips anything outside `[A-Za-z0-9_-]` so a malformed
`task_id` cannot escape the folder name. `close()` deletes only this
instance's folder.

### Action registry

```python
ACTION_REGISTRY = {
    "warmup":       execute_warmup,
    "upload_reels": execute_upload,
    "upload_post":  execute_upload,
    "upload_story": execute_upload,
    # extend here
}
```

Adding a new action = one entry in the registry plus a handler with
signature `(browser, args) -> dict`. Handlers must NOT instantiate or close
a browser, and must let exceptions propagate.

### Cookie normalization

Account cookies can be stored as either the IG-export list-of-dicts shape
or a flat `{name: value}` map. `TaskExecutor._normalize_cookies()` accepts
both and converts the flat shape into IG-domain cookie dicts.

---

## 6. Media & FFmpeg

### Storage layout

```
$MEDIA_ROOT/
├── originals/
│   └── <asset_id_hex>.<ext>     # raw uploads (parent Assets)
└── variants/
    └── <parent_asset_id>/
        ├── 001_<child_id_hex>.<ext>
        ├── 002_<child_id_hex>.<ext>
        └── ...
```

Configurable via `settings.MEDIA_ROOT` (defaults to `./media`).

### Upload — `POST /media/upload`
[`api/routers/media.py`](backend/app/api/routers/media.py)

Multipart form: `user_id` (UUID), optional `folder_id` (UUID), `file`
(`UploadFile`). The route streams the upload to disk in 1 MB chunks with
a hard cap (`MEDIA_MAX_UPLOAD_BYTES`, default 500 MB) — nothing is buffered
in RAM. Only an extension allowlist (`.mp4 / .mov / .m4v / .mkv / .webm`)
is enforced. On success, a parent `Asset` is created with `status=raw`.

### Uniqueize — `POST /media/{asset_id}/uniqueize?copies=N`

Queues the Celery task
[`uniqueize_video`](backend/app/workers/media_tasks.py). For each requested
copy, the worker spawns one `ffmpeg` process and writes a child `Asset`.

#### Anti-fraud bypass mix

| Lever | Purpose | How |
|---|---|---|
| `-map_metadata -1` | Strip every original tag (camera model, GPS, IG IDs) | flat removal |
| `-metadata comment=<uuid4.hex>` | Inject a unique fingerprint per clone | guarantees a fresh MD5 even with identical pixels |
| `-vf noise=alls=<r>:allf=t` | Add invisible per-frame temporal noise | `r ∈ [1, 4]` — defeats perceptual hashes |
| `-b:v <jittered>` | Slight bitrate change | random `0.95×..1.05×` of input bitrate (probed via `ffprobe`) |
| `-preset <random>` | Randomize the encoder behavior | `veryfast` / `faster` / `fast` / `medium` |
| `-movflags +faststart` | IG-friendly mp4 layout | moov atom written up front |

#### Lifecycle

```
parent.status: RAW
       │
       ▼ uniqueize starts
parent.status: PROCESSING        children: (none yet)
       │
       │ for each copy:
       │   - ffprobe input bitrate
       │   - random nonce / preset / noise / bitrate jitter
       │   - subprocess.run(ffmpeg, ...)  ← per-variant short DB tx
       │   - write child Asset (status=READY, parent_id=parent.id)
       ▼
parent.status: READY              children: [READY, READY, ...]
```

On failure: parent is rolled back to `RAW`, half-written variant files
are unlinked, the Celery task raises so the result backend records the
failure.

---

## 7. API Reference

All endpoints live under [`backend/app/api/routers/`](backend/app/api/routers/)
and are aggregated by [`api/main.py`](backend/app/api/main.py). The
FastAPI app is mounted as `app.api.main:app`. Docs auto-generate at `/docs`.

### Health

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness check |

### Users — [`routers/user.py`](backend/app/api/routers/user.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/users/` | Create user; auto-attaches `Subscription(tier="free")` |
| `GET` | `/users/` | List |
| `GET` | `/users/{id}` | Read |
| `PATCH` | `/users/{id}` | Update email / password |
| `DELETE` | `/users/{id}` | Cascade-delete |
| `GET` | `/users/{id}/subscription` | Current subscription |
| `PATCH` | `/users/{id}/subscription` | Tier upgrade |

### Proxies — [`routers/proxy.py`](backend/app/api/routers/proxy.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/proxies/` | |
| `GET` | `/proxies/` | |
| `GET` | `/proxies/{id}` | |
| `PATCH` | `/proxies/{id}` | |
| `DELETE` | `/proxies/{id}` | |

### Instagram Accounts — [`routers/account.py`](backend/app/api/routers/account.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/accounts/` | Tags get normalized (lowercase / strip / dedupe) |
| `GET` | `/accounts/?user_id=...` | Filter by owner |
| `GET` | `/accounts/{id}` | |
| `PATCH` | `/accounts/{id}` | Tag updates supported |
| `DELETE` | `/accounts/{id}` | |

### Tasks — [`routers/task.py`](backend/app/api/routers/task.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/tasks/` | Manual creation (rare) |
| `GET` | `/tasks/?account_id=...&status=...` | Filtered list |
| `GET` | `/tasks/{id}` | |
| `PATCH` | `/tasks/{id}` | Update status / payload / error_log |
| `DELETE` | `/tasks/{id}` | |

### AI — [`routers/ai.py`](backend/app/api/routers/ai.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/ai/generate-task` | **Side-effect free.** Returns `ParsedTaskPlan` (200). May contain `clarification_needed` |

### Orchestrator — [`routers/orchestrator.py`](backend/app/api/routers/orchestrator.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/orchestrator/accounts/{id}/validate` | Queue cookie-validation Celery task. `202 Accepted` |
| `POST` | `/orchestrator/tasks/{id}/start` | Start an existing single Task. `202 Accepted` |
| `POST` | `/orchestrator/tasks/fan-out` | **Plan → many Tasks → many Celery jobs.** `202 Accepted`. Returns `FanOutResponse` with `dispatched[]` and `skipped[]` |

### Media — [`routers/media.py`](backend/app/api/routers/media.py)

| Method | Path | Notes |
|---|---|---|
| `POST` | `/media/folders` | |
| `GET` | `/media/folders?user_id=...` | |
| `DELETE` | `/media/folders/{id}` | Cascades to assets |
| `GET` | `/media/assets?user_id=...&folder_id=...&parent_id=...&status=...` | Filtered list |
| `GET` | `/media/assets/{id}` | |
| `POST` | `/media/upload` | Multipart. Streams to disk with 500 MB cap |
| `POST` | `/media/{asset_id}/uniqueize?copies=N` | Queue FFmpeg job. Body `{"copies": N}` overrides query. `202 Accepted` |

---

## Appendix — Configuration

All runtime settings come from environment variables (loaded via
`pydantic-settings` from `backend/.env`). See
[`core/config.py`](backend/app/core/config.py).

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:password@localhost:5432/ig_crm` | SQLAlchemy DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Generic Redis (currently unused outside Celery) |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery result store |
| `CELERY_TASK_TIME_LIMIT` | `1800` | Hard kill (sec) |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `1500` | Soft `SoftTimeLimitExceeded` (sec) |
| `OPENAI_API_KEY` | `""` | Required for `/ai/generate-task` |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM used by `AIParser` |
| `MEDIA_ROOT` | `./media` | Filesystem root for uploads + variants |
| `MEDIA_MAX_UPLOAD_BYTES` | `524288000` | 500 MB upload cap |
| `FFMPEG_BIN` | `ffmpeg` | Binary on `PATH` |
| `FFPROBE_BIN` | `ffprobe` | Binary on `PATH` |
| `FFMPEG_TIMEOUT_SECONDS` | `900` | Per-variant subprocess timeout |
| `FFMPEG_DEFAULT_BITRATE_BPS` | `3000000` | Fallback when ffprobe can't read input |

## Appendix — Running the stack locally

```bash
# 1. Postgres + Redis (via your preferred docker-compose / brew services)

# 2. API
cd backend
uvicorn app.api.main:app --reload

# 3. Celery worker (in a second shell)
cd backend
celery -A app.core.celery_app.celery_app worker \
    --loglevel=info \
    -Q ig_crm.default \
    --include=app.workers.celery_tasks,app.workers.media_tasks
```

OpenAPI docs at `http://localhost:8000/docs`.
