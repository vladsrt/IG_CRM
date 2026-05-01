# Instagram CRM — Technical Architecture

> Senior-engineering reference for the IG CRM platform.
> Reflects the post Security & Reliability Sprint state (CRITICAL-1 → 4
> hardening landed). Read this before adding a new feature, writing a
> frontend integration, onboarding another agent, or reviewing PRs that
> touch the worker fleet.

---

## 1. System Overview

### 1.1 What it is

Instagram CRM is a multi-tenant SaaS platform that lets a single operator
drive a fleet of Instagram accounts through a conversational interface.
The operator types natural-language instructions ("warm up my crypto
accounts, then post the new reel everywhere by tonight"); the platform
plans the work with an LLM, the operator reviews and approves, and a
fleet of headless Chromium workers executes the plan against real
Instagram sessions through authenticated proxies.

### 1.2 Target scale

| Dimension | Design target | Current bottleneck |
|---|---|---|
| Tenants (Users) | $10^4$ | none — Postgres scales linearly |
| Instagram accounts per tenant | $10^3$ | none |
| Concurrent browser sessions per worker host | 4–8 | Chromium RAM (~1 GB / instance) |
| Tasks dispatched per minute (steady state) | $10^2$–$10^3$ | per-account exclusion (intentional) |
| Latency budget — AI plan generation | < 8 s p95 | OpenAI Structured Outputs round-trip |
| Latency budget — task dispatch (fan-out) | < 2 s for 50 accounts | trust probe + DB writes (parallelizable) |

Throughput is **deliberately throttled per-account** by a pessimistic row
lock (see §6) — overlapping sessions on the same Instagram identity
trigger Meta's session-anomaly detector and burn the account. Account-level
serialization is a *correctness* property, not a performance constraint.

### 1.3 Billing model

Subscription-based: Free / Pro / Enterprise. There is no token-balance
metering. A `User` row owns exactly one `Subscription` row, auto-provisioned
at signup with `tier="free"`. See [`models/billing.py`](backend/app/models/billing.py).

---

## 2. Technology Stack

| Layer | Component | Choice | Rationale |
|---|---|---|---|
| HTTP / Orchestration | API framework | **FastAPI** | Async-native, OpenAPI auto-docs, Pydantic v2 native, maps directly onto our Structured Outputs contract |
| HTTP / Orchestration | Validation | **Pydantic v2** | Same models reused as OpenAI `response_format` schemas — single source of truth for the AI contract |
| Distributed task engine | Worker scheduler | **Celery 5** | Mature retry semantics, per-task time limits, Beat scheduler for janitors, Redis backend for low ops overhead |
| Distributed task engine | Broker + result backend | **Redis 7** | Single dependency for queue + result store; pub-sub primitives ready for future event streams |
| State + persistence | RDBMS | **PostgreSQL 15** | JSONB for tags / payloads, `FOR UPDATE NOWAIT` for the per-account exclusion contract, native UUID type |
| State + persistence | ORM | **SQLAlchemy 2.0 (sync)** | Sync session model maps cleanly onto Celery worker threads; 2.0 typed `Mapped[...]` syntax is mypy-friendly |
| Schema migrations | Migration tool | **Alembic** | Standard for SQLAlchemy; checked-in under `migrations/` |
| Browser automation | Driver | **DrissionPage** | Lightweight Chromium control without Selenium grid; supports network listening + raw mouse coordinate moves needed for bezier trajectories |
| Browser automation | Proxy auth | **Generated MV2 extension** | Authenticated HTTP proxies cannot be configured via Chromium CLI; a per-instance unpacked extension is the only reliable path |
| AI planning | LLM provider | **OpenAI (`gpt-4o-mini` default)** | Strict Structured Outputs guarantees a Pydantic-validated plan with no JSON parsing in our code path |
| Media processing | Encoder | **FFmpeg / FFprobe** | Per-clone bitrate jitter, noise filter, metadata stripping for anti-fingerprinting |
| Password hashing | KDF | **PBKDF2-HMAC-SHA256 (stdlib)** | Zero external dependency for the auth path; replaceable with bcrypt / argon2 |

### 2.1 Conscious omissions

- **No third-party HTTP client (`requests` / `httpx`)** — `urllib` covers the trust-probe path and avoids a transitive-dep surface.
- **No Selenium grid** — DrissionPage is sufficient; a grid would add ops cost without solving any modeled problem.
- **No GraphQL** — REST + Pydantic gives us the typed contract we need.

---

## 3. High-Level Architecture

```
                              ┌──────────────────────────┐
   Operator (browser)  ─────► │  Frontend (chat UI)       │
                              └─────────────┬────────────┘
                                            │ 1. POST /ai/generate-task
                                            ▼
                              ┌──────────────────────────┐
                              │  FastAPI                 │
                              │   ├─ /ai (no DB writes)  │ ──► OpenAI
                              │   ├─ /orchestrator       │
                              │   ├─ /accounts /tasks    │
                              │   └─ /media              │
                              └────┬─────────────────────┘
                                   │ 2. POST /orchestrator/tasks/fan-out
                                   │    (operator-approved plan)
                                   ▼
                              ┌──────────────────────────┐
                              │  Pre-flight gates         │
                              │   • trust score (proxy +  │
                              │     UA + hygiene)         │
                              │   • spintax + link        │
                              │     uniqueization         │
                              └────┬─────────────────────┘
                                   │ 3. one Task row per account
                                   ▼
                              ┌──────────────────────────┐
                              │  PostgreSQL              │
                              │   • Tasks  (FSM)         │
                              │   • Accounts (FOR UPDATE)│
                              │   • account_metrics      │
                              │   • assets / variants    │
                              └────┬─────────────────────┘
                                   │ 4. run_instagram_task.delay(task_id)
                                   ▼
                              ┌──────────────────────────┐
                              │  Redis broker             │
                              └────┬─────────────────────┘
                                   │ 5. picked up by worker
                                   ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  Celery Worker Process (Linux host, headless Chromium)         │
   │                                                                │
   │  ┌─ Orphan recovery + account row lock (CRITICAL-1, CRITICAL-2)│
   │  │                                                             │
   │  ▼                                                             │
   │  TaskExecutor                                                  │
   │   ├─ InstagramBrowser (proxy + cookies + unique plugin folder)│
   │   ├─ HumanBehaviorEngine (bezier mouse, typing cadence)       │
   │   ├─ ObservabilityMonitor (URL poll + network listener)       │
   │   └─ ACTION_REGISTRY → upload / warmup / update_profile / ... │
   │                                                                │
   │  ▼ on completion: persist metrics → run shadowban detector    │
   └───────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                              ┌──────────────────────────┐
                              │  Celery Beat              │
                              │   • reap_stale_tasks /5m  │
                              └──────────────────────────┘
```

### 3.1 Layer responsibilities

| Layer | Owns | Does NOT own |
|---|---|---|
| **API / FastAPI** | HTTP contract, request validation, fan-out orchestration, pre-flight gates | Browser automation, ffmpeg, retry policy |
| **Celery + Redis** | Task queueing, retries, time limits, scheduling | Business state (lives in Postgres) |
| **PostgreSQL + SQLAlchemy** | Single source of truth for state, FSM transitions, row-level locking | Side effects (those happen in workers) |
| **Worker fleet (DrissionPage)** | Browser session, action handlers, network interception, FFmpeg subprocesses | HTTP routing, plan generation |

The strict separation means each layer can be redeployed independently:
roll out a new worker image without touching the API; scale Postgres
read replicas without involving Celery; A/B-test a new LLM model behind
the `/ai` endpoint without redeploying workers.

---

## 4. Subsystem Deep-Dives

### 4.1 FastAPI Orchestrator

The HTTP layer is **stateless**. Every endpoint reads from / writes to
Postgres and dispatches to Redis; nothing is held in process memory
between requests.

**Routers** ([`backend/app/api/routers/`](backend/app/api/routers/)):

| Router | Purpose | Side effects |
|---|---|---|
| `user.py` | CRUD for tenants; auto-provisions free Subscription | DB writes |
| `account.py` | CRUD for IG accounts; tag normalization | DB writes |
| `proxy.py` | CRUD for proxy pool | DB writes |
| `task.py` | CRUD for Tasks (manual / debug path) | DB writes |
| `ai.py` | LLM plan generation | **None** — read-only OpenAI call |
| `orchestrator.py` | Fan-out, single-task start, account validation | DB writes + Celery dispatch |
| `media.py` | Folder + asset CRUD, raw upload, uniqueize trigger | DB writes + filesystem + Celery dispatch |

**Why FastAPI:** the AI endpoint contract leans heavily on Pydantic v2.
The same `ParsedTaskPlan` model is passed to OpenAI's
`client.beta.chat.completions.parse(response_format=...)` AND used as the
FastAPI response schema — one declaration, two enforcements.

### 4.2 Celery + Redis Task Engine

Two task families:

**Browser tasks** ([`app/workers/celery_tasks.py`](backend/app/workers/celery_tasks.py)):
- `validate_account_session(account_id)` — cookie validity check (placeholder for full validator action)
- `run_instagram_task(task_id)` — the main browser dispatch
- `reap_stale_tasks()` — janitor (see §6)

**Media tasks** ([`app/workers/media_tasks.py`](backend/app/workers/media_tasks.py)):
- `uniqueize_video(asset_id, copies)` — FFmpeg-based per-clone uniqueization

**Configuration** ([`app/core/celery_app.py`](backend/app/core/celery_app.py)):

| Setting | Value | Why |
|---|---|---|
| `task_acks_late=True` | enabled | Re-deliver on worker death; pairs with orphan recovery (§6) |
| `task_reject_on_worker_lost=True` | enabled | Hard kills (SIGKILL) properly requeue |
| `worker_prefetch_multiplier=1` | 1 | One in-flight task per worker; required because each task can hold a Chromium subprocess for minutes |
| `task_time_limit=1800` | 30 min | Hard ceiling — `SIGKILL` enforced beyond this |
| `task_soft_time_limit=1500` | 25 min | Raises `SoftTimeLimitExceeded` first, giving the executor a window to mark FAILED cleanly |
| `beat_schedule` | `reap_stale_tasks` every 5 min | Janitor for the orphan-recovery contract |

**Why Celery + Redis (vs. RabbitMQ, SQS, Temporal):** Redis is already
in our stack as a generic cache target; running Celery on it removes the
need for a second messaging system. We don't need RabbitMQ's quorum-queue
durability — task state is in Postgres, not the queue.

### 4.3 PostgreSQL + SQLAlchemy State Layer

PostgreSQL is the **single source of truth** for every business object.
Celery's result backend is treated as ephemeral; the operator's view of
"what's running" comes from `Task.status`, never from Celery's internal
state.

**Models** ([`backend/app/models/`](backend/app/models/)):

| Model | Purpose | Notable columns |
|---|---|---|
| `User` | Platform tenant | `email` (unique), `hashed_password` (PBKDF2) |
| `Subscription` | Billing tier | `tier` (`free` / `pro` / `enterprise`), `valid_until` |
| `InstagramAccount` | One IG identity | `cookies` JSONB, `tags` JSONB (lowercase array), `status` (FSM-ish string) |
| `Proxy` | Authenticated proxy | rendered as `user:pass@host:port` (no scheme prefix) |
| `Task` | Unit of execution | `payload` JSONB (the full ParsedTaskPlan), `status` FSM, `priority` int |
| `Asset` | Media file on disk | `parent_id` (self-FK for variants), `status` FSM (`raw`/`processing`/`ready`) |
| `MediaFolder` | Asset grouping | `name` |
| `AccountMetric` | Time-series metric | `(account_id, metric_type, captured_at)` indexed; rows from network interception |

**Why sync SQLAlchemy:** Celery workers are thread-pool processes. Sync
sessions map 1:1 onto worker threads with no `asyncio` event-loop
gymnastics. The HTTP layer's nominal latency cost (sync DB inside a sync
route handler running in a thread) is irrelevant — we're never blocking
on DB beyond a few ms per request.

**Tag targeting (load-bearing):** `InstagramAccount.tags` is a JSONB
array. The fan-out orchestrator queries via JSONB containment
(`tags @> '["crypto"]'::jsonb`). For O(log n) lookup at scale, add a
GIN index:

```sql
CREATE INDEX ix_accounts_tags ON instagram_accounts USING GIN (tags);
```

### 4.4 DrissionPage Worker Fleet

**`InstagramBrowser`** ([`workers/core/browser_core.py`](backend/workers/core/browser_core.py))
is the lifecycle owner of one Chromium process per task. It:

1. Generates a per-instance unpacked Chrome extension (MV2) for proxy auth
2. Launches headless Chromium with that extension, custom UA, image-loading disabled
3. Injects cookies after navigating to `/robots.txt` to set the IG domain context
4. Tears down both Chromium AND the on-disk extension folder on `close()`

**`TaskExecutor`** ([`workers/core/executor.py`](backend/workers/core/executor.py))
owns the per-task pipeline:

1. Construct `InstagramBrowser` (with `task_id` so the plugin folder is unique)
2. Inject cookies
3. Start `ObservabilityMonitor` (URL poll + network listener)
4. Iterate `payload["commands"]`, dispatching each through `ACTION_REGISTRY`
5. Between commands: `monitor.check_checkpoint()` — abort if IG redirected to `/challenge/`
6. `try/finally` guarantee: `browser.close()` always runs, even on handler crash
7. Drain captured metrics → flush to `account_metrics` → run shadowban detector

**Action registry** (current handlers):

| Action key | Handler | Notes |
|---|---|---|
| `warmup` | `execute_warmup` | Randomized feed scroll (low-risk activity) |
| `upload_reels` / `upload_post` / `upload_story` | `execute_upload` | One handler — IG decides server-side based on duration / aspect ratio |
| `update_profile` | `execute_update_profile` | Bio, avatar, privacy toggle |

Adding a handler = one entry in `ACTION_REGISTRY` + a function with
signature `(browser, args) -> dict`. Handlers must NOT instantiate or
close a browser, and must let exceptions propagate.

**Why DrissionPage (not Selenium / Playwright):**
- Native Chromium control without a WebDriver server hop
- First-class `page.listen` API for network interception (Epic 6)
- Synchronous API matches the Celery worker model
- Smaller dependency footprint than Playwright

---

## 5. The Human Behavior Engine

[`workers/core/behavior.py`](backend/workers/core/behavior.py) replaces
every raw DrissionPage interaction (`ele.click()`, `ele.input()`, `move_to(target, duration=0)`)
with timing- and trajectory-shaped equivalents designed to defeat
Meta's bot-detection heuristics.

### 5.1 Bezier mouse trajectories

```
┌─ Why ──────────────────────────────────────────────────────────────────┐
│ A native ele.click() teleports the cursor to the element's center and │
│ dispatches mousedown/mouseup with no path history. IG telemetry sees   │
│ a single mousemove event with origin=destination — a strong bot signal.│
└───────────────────────────────────────────────────────────────────────┘
```

Every `behavior.move_to(target)` call:

1. Resolves `target` to an absolute `(x, y)` (element midpoint or coord tuple).
2. With probability `~18%`, generates an **overshoot** point (±22 px), draws the curve to *that*, micro-pauses, then draws a *fast* corrective curve to the real target. Looks exactly like missing a click and adjusting.
3. Otherwise generates a single cubic Bezier from current cursor to target. Two control points are placed at 1/3 and 2/3 along the segment, offset perpendicular by `~10–28% × distance` on randomly chosen sides — mostly opposite (S-curve), occasionally same side (one-sided arc). Real wrist motion does both.
4. Samples 22–42 points along the curve (scaled down for hops <60 px) and walks them via `page.actions.move_to((x, y), duration=0)`, with a per-step `time.sleep` shaped by an **ease-in/out cubic** (slow at endpoints, fastest near `t=0.5`).

`behavior.click(target)` calls `move_to`, then `page.actions.click()` —
which dispatches `mousedown`/`mouseup` AT the current cursor position,
preserving the trajectory.

### 5.2 Variable-cadence typing

`behavior.type_into(target, text)` types one character at a time with:
- Inter-key delay `random.uniform(0.055, 0.18) s`
- 5% probability of a "thinking" pause `random.uniform(0.45, 1.2) s`
  inserted between any two keystrokes

This produces a keystroke timing distribution that matches real users
within standard ML detection thresholds.

### 5.3 Reading pauses (`behavior.read_pause(content_length)`)

Sleeps `content_length / 22 chars-per-sec` (≈250 wpm) ±30% jitter,
clamped to `[0.6 s, 12 s]`. A 20-char caption gets a ~1 s pause; a 400-char
one gets ~12 s. Used after navigation, after typing a caption, before
the Share button.

### 5.4 Micro-scrolls (`behavior.micro_scroll()`)

Emits 1–3 wheel deltas of 60–240 px each, 70% downward / 30% upward
correction. Used between actions to mimic the skim-then-act behavior of
real users. A pure-bot session never micro-scrolls; the absence is itself
a signal.

### 5.5 Determinism for tests

The engine accepts an optional `rng_seed` constructor argument that seeds
its `random.Random` instance. Tests get reproducible trajectories;
production passes `None` (true randomness via `SystemRandom`).

---

## 6. Reliability & Concurrency Model

This section documents the **load-bearing contracts** introduced by the
Security & Reliability Sprint (CRITICAL-1, CRITICAL-2). Both contracts
are enforced at the database level — they do not depend on cooperating
workers behaving correctly.

### 6.1 The per-account exclusion invariant

> **At most one browser session per `InstagramAccount.id` is RUNNING at any instant, across all workers in the fleet.**

**Why:** Two Chromium instances logged in with the same `sessionid`
cookie emit overlapping interactions to Instagram. Meta's session-anomaly
detector treats this as account compromise, serves a `/challenge/`, and
the account is gone. The shadowban detector is irrelevant at that point —
the row is already burned.

**How:** `run_instagram_task` performs a pessimistic lock with NOWAIT
semantics on the InstagramAccount row, plus a sibling-RUNNING check, ALL
inside a single transaction:

```python
with SessionLocal() as db:
    task = db.get(Task, task_uuid)
    if task.status not in {"pending", "draft"}:
        return {"status": "non_runnable"}

    try:
        account = db.execute(
            select(InstagramAccount)
            .where(InstagramAccount.id == task.account_id)
            .with_for_update(nowait=True)        # ← lock
        ).scalar_one_or_none()
    except OperationalError:
        # Sibling worker holds the row. Back off exponentially.
        raise self.retry(
            countdown=30 * (2 ** self.request.retries),
            max_retries=5,
        )

    sibling = db.execute(
        select(Task.id)
        .where(
            Task.account_id == account.id,
            Task.status == TaskStatus.RUNNING.value,
            Task.id != task_uuid,
        )
        .limit(1)
    ).scalar_one_or_none()
    if sibling is not None:
        raise self.retry(...)

    # Mark RUNNING inside the locked transaction so a sibling sees us
    # the moment the lock releases on commit.
    task.status = TaskStatus.RUNNING.value
    db.commit()
```

**Retry budget:** 5 attempts at exponential backoff (30, 60, 120, 240,
480 s = ~16 min total). Beyond that the Task is marked FAILED.
Sixteen minutes of contention is itself a signal worth surfacing.

**Belt and braces:** the FOR UPDATE lock alone would be insufficient if
a sibling's Postgres connection died without releasing the lock (the
row appears unlocked but the sibling Task is still in RUNNING state).
The sibling-check covers exactly that case.

### 6.2 Orphan recovery — the at-task-start contract

> **A Task that arrives for execution with `status='RUNNING'` is treated as the corpse of a previous worker and refused.**

**Why:** The combination of `acks_late=True` + a hard worker death
(OOM kill, SIGKILL, host eviction) re-delivers the same task message
to a fresh worker while the DB row still says RUNNING. Naive re-execution
would cause a duplicate Instagram side-effect (double post, double DM).

**How:** [`celery_tasks._recover_orphan_if_needed()`](backend/app/workers/celery_tasks.py)
runs as the very first thing in `run_instagram_task`:

```python
with SessionLocal() as db:
    task = db.get(Task, task_uuid)
    if task.status in _ORPHAN_STATUSES:        # = {"running"}
        task.status = TaskStatus.FAILED.value
        task.error_log = (
            "Orphan recovery: previous worker died with status RUNNING. "
            "Refusing automatic re-execution to prevent duplicate "
            "Instagram side-effects. Re-dispatch manually if intended."
        )
        db.commit()
        return {"status": "orphan_recovered"}
```

**The operator sees the failure with a clear cause, and the message is
acked.** Re-dispatching the task is an explicit human decision.

### 6.3 The `reap_stale_tasks` janitor

> **Even if no requeue ever occurs (broker reconnect failure, host hard-reboot), no Task can stay in RUNNING beyond a configurable threshold.**

Companion to orphan recovery, scheduled by Celery Beat every 5 min:

```python
@celery_app.task(name="ig_crm.reap_stale_tasks")
def reap_stale_tasks(stale_after_seconds: int | None = None) -> dict:
    threshold = stale_after_seconds or _REAP_AFTER_SECONDS  # 1 hour default
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)
    with SessionLocal() as db:
        stuck = db.execute(
            select(Task).where(
                Task.status == TaskStatus.RUNNING.value,
                Task.created_at < cutoff,
            )
        ).scalars()
        for task in stuck:
            task.status = TaskStatus.FAILED.value
            task.error_log = (
                f"Reaped by janitor after {threshold}s in RUNNING — "
                "presumed dead worker."
            )
        db.commit()
```

Operationally:
```bash
# In a third shell alongside worker + API
celery -A app.core.celery_app.celery_app beat --loglevel=info
```

Without `beat`, the at-task-start orphan recovery still works on the next
delivery — the janitor is the safety net for cases where no delivery ever
happens.

### 6.4 Browser teardown invariant (Story 4.4 + CRITICAL-3)

> **Every Chromium subprocess launched by `InstagramBrowser` is reaped by `close()` before the worker thread returns control.**

Two layers of defense:

1. **`TaskExecutor.execute()` `try/finally`** — guarantees `browser.close()`
   runs even if a handler raises:
   ```python
   try:
       browser = InstagramBrowser(...)
       # ... commands ...
   finally:
       if browser is not None:
           try:
               browser.close()
           except Exception:
               logger.exception("teardown raised; continuing")
   ```

2. **`InstagramBrowser.__init__` self-cleanup** — handles the case where
   construction itself fails partway through (Chromium spawned, but a
   later line raised). Cleanup-relevant attributes are initialized to
   safe defaults BEFORE the first line that can raise; the entire setup
   is wrapped in try/except that calls `self.close()` and re-raises:
   ```python
   def __init__(self, ...):
       # safe defaults so close() works on partial construction
       self.page = None
       self.plugin_path = ""
       try:
           self.plugin_path = create_proxy_extension(...)
           self.co = ChromiumOptions()
           # ... configure ...
           self.page = ChromiumPage(self.co)
       except Exception:
           try:
               self.close()
           except Exception:
               pass  # nested cleanup must not mask original
           raise
   ```

`close()` itself uses `getattr(..., default)` so it's idempotent and safe
on partially-constructed instances. After cleanup it nulls the refs, so
a follow-up `close()` is a cheap no-op.

### 6.5 Per-task plugin folder isolation (Story 4.5)

The historical `InstagramBrowser` hard-coded a single `runtime_proxy_plugin`
folder name, so two concurrent tasks in the same worker process collided
on disk. The current code derives the folder name from `task_id`:

```python
self.plugin_folder = f"runtime_proxy_plugin_{sanitize(task_id) or uuid4().hex}"
```

`_sanitize_token()` strips anything outside `[A-Za-z0-9_-]` so a
malformed `task_id` cannot escape the folder name. `close()` deletes
only this instance's folder.

### 6.6 Reliability summary

| Failure mode | Defense | Owner |
|---|---|---|
| Worker OOM / SIGKILL during browser run | Orphan recovery refuses requeued message | `celery_tasks._recover_orphan_if_needed` |
| Worker dies without requeue (host reboot) | `reap_stale_tasks` janitor every 5 min | Celery Beat |
| Two tasks dispatched to same account | `FOR UPDATE NOWAIT` + sibling check | Postgres + `run_instagram_task` |
| Handler crash mid-execution | `TaskExecutor` `try/finally` calls `browser.close()` | `executor.py` |
| `InstagramBrowser.__init__` partial failure | Wrapped try/except calls `self.close()` and re-raises | `browser_core.py` |
| Concurrent plugin-folder collision | Per-instance unique folder name from `task_id` | `browser_core.py` |
| IG checkpoint mid-task | `ObservabilityMonitor` URL poll → `CheckpointException` | `observability.py` |

---

## 7. Security Framework

### 7.1 Path traversal protection (CRITICAL-4)

> **Any operator-controlled file path consumed by the worker layer is validated against `settings.MEDIA_ROOT` before reaching DrissionPage or `subprocess`.**

**Threat model:** `Task.payload` is operator-supplied JSONB (the AI plan
the operator approved). Anyone able to write to `Task.payload` (operator
account, anyone with API access in the current pre-auth state, anyone
with a Postgres connection) could set `args.file_path = "/etc/passwd"`.
The worker would dutifully POST it to Instagram. This is a one-API-call
data-exfiltration primitive.

**Defense:** [`workers/core/safety.py`](backend/workers/core/safety.py)
exposes a single function:

```python
def resolve_within_media_root(candidate: str, media_root: str) -> str:
    root = Path(media_root).expanduser().resolve(strict=True)
    target = Path(candidate).expanduser().resolve(strict=True)
    if not target.is_file():
        raise UnsafePathError(...)
    target.relative_to(root)        # raises ValueError → UnsafePathError
    return str(target)
```

Critical detail: `Path.resolve(strict=True)` follows symlinks BEFORE the
`relative_to` check. A symlink under `MEDIA_ROOT` pointing at
`/etc/hostname` is therefore correctly rejected — `os.path.abspath`
alone would have missed this.

**Call sites:**
- [`action_upload.py::execute_upload`](backend/workers/actions/action_upload.py) — at the top, AND inside `_inject_file` (defense in depth)
- [`action_update_profile.py::execute_update_profile`](backend/workers/actions/action_update_profile.py) — at the top, AND inside `_set_avatar`

Both raise their respective `*ActionError` on a violation, so the failure
surfaces as `Task.error_log` content with a clear message.

### 7.2 Browser leak prevention (CRITICAL-3)

Covered in §6.4. The invariant is: **Chromium subprocess orphans are
impossible from within the application's control path.** The only
remaining vector is host-level SIGKILL between Chromium spawn and Python
ref binding — a defense-in-depth `pkill -f chromium` cron is recommended
for long-lived worker hosts.

### 7.3 Network metric interception (Epic 6)

[`workers/core/observability.py`](backend/workers/core/observability.py)
runs two daemon threads attached to the live `ChromiumPage`:

**URL-change poller (Task 6.2):** Polls `page.url` on a 1-second cadence.
If the URL matches `/challenge/` or `/accounts/suspended/`, sets a
thread-safe flag. `TaskExecutor` calls `monitor.check_checkpoint()`
between commands; the flag raises `CheckpointException`, the executor
unwinds cleanly, and `run_instagram_task` marks the Task FAILED + the
account `checkpoint_required`.

**Network listener (Task 6.1):** Subscribes to IG GraphQL / private-API
JSON responses via `page.listen`. A bounded recursive walk over each
response body extracts `follower_count`, `reach`, and reel `play_count` /
`view_count` into typed `MetricSample` records. The executor drains the
queue after the task completes and persists rows to `account_metrics`.

**Why this matters for security:** the metric layer is what feeds the
**shadowban detector** ([`app/services/shadowban.py`](backend/app/services/shadowban.py)).
Combined with the trust score (§7.4), it gives us a feedback loop —
flagged accounts automatically poison subsequent dispatch attempts via
the trust gate's hygiene check.

### 7.4 Trust score pre-flight gate (Epic 8.1)

Before every fan-out dispatch, [`app/services/trust.py`](backend/app/services/trust.py)
computes a `0–100` trust score per account from three weighted signals:

| Signal | Weight | Failure mode |
|---|---|---|
| Proxy reachability | 60 | DNS lookup + HEAD via authenticated proxy; hard-fail (score=0) on connection error |
| User-agent shape | 25 | Regex match against a recognized desktop browser pattern, length guard |
| Account hygiene | 15 | Penalize `possible_shadowban` tag, `checkpoint_required` status |

Below `DEFAULT_MIN_TRUST_SCORE = 50` the dispatch refuses — the account
goes into `skipped[]` with reason `low_trust_score (score=X/100): <details>`.
The proxy weight alone exceeds the threshold, encoding **a working proxy
is necessary but not sufficient**.

### 7.5 Spintax + link uniqueization (Epic 8.2 + 8.3)

[`app/services/spintax.py`](backend/app/services/spintax.py) runs once
per fan-out account, producing a unique `Task.payload`:

- `{a|b|c}` syntax is resolved per-clone with a fresh RNG.
- URLs in resolved text are rewritten through `obfuscate_link()` (placeholder for a real redirect service) so each clone's caption has a distinct short-link.
- Single-option groups like `{username}` are preserved verbatim — the engine requires at least one `|` to consider a group spintax, leaving downstream templating placeholders alone.
- Input is bounded (`8000` char cap, max-`|` count cap) to prevent DoS via algorithmic complexity.

The result: **50 accounts dispatched to from the same operator-approved
plan produce 50 byte-distinct Task payloads.**

### 7.6 Open security items (transparently logged)

| Item | Status | Mitigation |
|---|---|---|
| **No authentication on any endpoint** | Open | API trusts `user_id` form fields. JWT middleware before public deployment. |
| **`ig_password` and `cookies` stored plaintext** | Open | Application-layer Fernet envelope encryption planned (`app/core/secrets.py`). |
| **`obfuscate_link` is a placeholder** | Open | Wire to a real redirect service before any production link traffic. |
| **Stored XSS via `Asset.metadata_['original_filename']`** | Open | Sanitize at upload boundary; one-line fix flagged. |

These four are documented in the audit report. They block "production-ready"
in the public-deployment sense; they do NOT block internal pilots.

---

## 8. Task Lifecycle (End-to-End)

A complete trace from operator prompt to executed Instagram action.

```
┌───────────────────────────────────────────────────────────────────────┐
│ 1. Operator types: "post the new reel to all my crypto accounts       │
│    tonight, hide the like count"                                      │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 2. POST /ai/generate-task                                             │
│    - AIParser.parse() calls OpenAI with response_format=ParsedTaskPlan│
│    - Returns:                                                         │
│        { summary: "...",                                              │
│          priority: "high",                                            │
│          target_tags: ["crypto"],                                     │
│          clarification_needed: null,                                  │
│          commands: [{action: "upload_reels", args: [...]}] }          │
│    - 200 OK. Zero DB writes.                                          │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 3. Frontend renders the plan. Operator approves.                      │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 4. POST /orchestrator/tasks/fan-out                                   │
│    - Pre-flight: refuses if clarification_needed != null              │
│    - Resolves accounts: tags ∪ explicit IDs (deduped)                 │
│    - For each account:                                                │
│        a. Trust score gate (proxy probe + UA + hygiene)               │
│        b. Spintax + link uniqueization on a payload clone             │
│        c. crud_task.create_task(status=PENDING, payload=unique)       │
│        d. run_instagram_task.delay(str(task.id))                      │
│    - 202 Accepted with FanOutResponse: dispatched[] + skipped[]       │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 5. Celery worker picks up run_instagram_task(task_id)                 │
│    - Orphan recovery (CRITICAL-1): refuse if RUNNING                  │
│    - Lock account row (CRITICAL-2): FOR UPDATE NOWAIT + sibling check │
│    - Mark Task.status = RUNNING (inside the locked transaction)       │
│    - Build payload dict, commit + release lock                        │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 6. TaskExecutor(payload).execute()                                    │
│    - Spawn InstagramBrowser (per-task plugin folder, proxy ext)       │
│    - Inject cookies                                                   │
│    - Start ObservabilityMonitor (URL poll + network listener)         │
│    - For each command:                                                │
│        - check_checkpoint()                                           │
│        - dispatch via ACTION_REGISTRY[command["action"]]              │
│            ├─ HumanBehaviorEngine drives bezier mouse + typing        │
│            └─ All file paths through resolve_within_media_root()      │
│    - finally: browser.close() + monitor.stop() + drain_samples()      │
│    - Persist samples to account_metrics                               │
│    - Run shadowban detector                                           │
└────────────────────────────┬──────────────────────────────────────────┘
                             ▼
┌───────────────────────────────────────────────────────────────────────┐
│ 7. On success: Task.status = COMPLETED, return result dict.           │
│    On CheckpointException: Task FAILED, account checkpoint_required.  │
│    On any other exception: Task FAILED with traceback in error_log.   │
│    Always: ack the Celery message.                                    │
└───────────────────────────────────────────────────────────────────────┘
```

### 8.1 State transitions

```
                      orphan-recovered (rare)
                              │
                              ▼
DRAFT ─────► PENDING ──────► RUNNING ──────► COMPLETED
   ▲             │              │
   │             │              ├──► FAILED  (handler crash, traceback in error_log)
   │             │              ├──► FAILED  (CheckpointException, account flagged)
   │             │              ├──► FAILED  (SoftTimeLimitExceeded)
   │             │              └──► FAILED  (reaped by janitor after 1 hour)
   └─────────────┘
   (broker dispatch failed → rolled back to DRAFT for retry)
```

### 8.2 What survives a worker death at each stage

| Worker dies at | What survives | What needs operator action |
|---|---|---|
| Before lock acquisition | Task in PENDING | Auto-retried (Celery requeue) |
| After lock + RUNNING flip, before browser spawn | Task in RUNNING; no browser | Orphan-recovered → FAILED on requeue OR reaped after 1h |
| Mid-browser session | Task in RUNNING; possibly an orphan Chromium | Same as above + host-level cron cleanup |
| After browser teardown, before COMPLETED commit | Task in RUNNING (or rarely orphan-zone) | Same as above |
| After COMPLETED commit | Task in COMPLETED | Nothing — work was real |

---

## 9. API Reference (concise)

All endpoints under [`backend/app/api/routers/`](backend/app/api/routers/),
aggregated by [`api/main.py`](backend/app/api/main.py). Mounted as
`app.api.main:app`. OpenAPI docs auto-generate at `/docs`.

| Router | Method | Path | Notes |
|---|---|---|---|
| meta | `GET` | `/health` | Liveness |
| user | `POST` `GET` `PATCH` `DELETE` | `/users/...` | Auto-attaches free Subscription on create |
| user | `GET` `PATCH` | `/users/{id}/subscription` | Tier read / upgrade |
| proxy | `POST` `GET` `PATCH` `DELETE` | `/proxies/...` | |
| account | `POST` `GET` `PATCH` `DELETE` | `/accounts/...` | Tag normalization on write |
| task | `POST` `GET` `PATCH` `DELETE` | `/tasks/...` | Manual / debug path |
| ai | `POST` | `/ai/generate-task` | Side-effect free; returns `ParsedTaskPlan` (200) |
| orchestrator | `POST` | `/orchestrator/accounts/{id}/validate` | Queue cookie validation (202) |
| orchestrator | `POST` | `/orchestrator/tasks/{id}/start` | Single-task dispatch (202) |
| orchestrator | `POST` | `/orchestrator/tasks/fan-out` | Plan → many Tasks → many Celery jobs (202) |
| media | `POST` `GET` `DELETE` | `/media/folders/...` | |
| media | `GET` | `/media/assets/...` | |
| media | `POST` | `/media/upload` | Multipart, 500 MB cap, streamed to disk |
| media | `POST` | `/media/{asset_id}/uniqueize?copies=N` | Queue FFmpeg job (202) |

---

## Appendix A — Configuration

All runtime settings come from environment variables (loaded via
`pydantic-settings` from `backend/.env`). See
[`core/config.py`](backend/app/core/config.py).

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:password@localhost:5432/ig_crm` | SQLAlchemy DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Generic Redis |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery result store |
| `CELERY_TASK_TIME_LIMIT` | `1800` | Hard kill (sec) |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `1500` | Soft `SoftTimeLimitExceeded` (sec) |
| `OPENAI_API_KEY` | `""` | Required for `/ai/generate-task` |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM used by `AIParser` |
| `MEDIA_ROOT` | `./media` | Filesystem root for uploads + variants (load-bearing for path-traversal defense) |
| `MEDIA_MAX_UPLOAD_BYTES` | `524288000` | 500 MB upload cap |
| `FFMPEG_BIN` | `ffmpeg` | Binary on `PATH` |
| `FFPROBE_BIN` | `ffprobe` | Binary on `PATH` |
| `FFMPEG_TIMEOUT_SECONDS` | `900` | Per-variant subprocess timeout |
| `FFMPEG_DEFAULT_BITRATE_BPS` | `3000000` | Fallback when ffprobe can't read input |

---

## Appendix B — Operational Runbook

### B.1 Local development

```bash
# 1. Postgres + Redis (docker-compose, brew services, etc.)

# 2. API
cd backend
uvicorn app.api.main:app --reload

# 3. Celery worker (second shell)
cd backend
celery -A app.core.celery_app.celery_app worker \
    --loglevel=info \
    -Q ig_crm.default \
    --include=app.workers.celery_tasks,app.workers.media_tasks

# 4. Celery Beat — REQUIRED for the reap_stale_tasks janitor (third shell)
cd backend
celery -A app.core.celery_app.celery_app beat --loglevel=info
```

OpenAPI docs at `http://localhost:8000/docs`.

### B.2 Deployment checklist

1. **`MEDIA_ROOT` exists on disk** before workers start. Without it,
   `resolve_within_media_root` raises `UnsafePathError("media_root does not exist")`
   on every upload — a loud, immediate, security-correct failure.
2. **Beat must run alongside the worker.** Without it, the orphan-recovery
   still works on next delivery, but silent-death rows (no requeue) are
   never swept.
3. **GIN index on `instagram_accounts.tags`** for tag-targeting at scale:
   ```sql
   CREATE INDEX ix_accounts_tags ON instagram_accounts USING GIN (tags);
   ```
4. **Defense-in-depth `pkill -f chromium` cron** on long-lived worker
   hosts — closes the residual SIGKILL-between-spawn-and-bind window.
5. **Set `OPENAI_API_KEY`** in your secrets manager (Vault, AWS SM,
   sealed-secrets), not in `.env`.
6. **Migrations** (`alembic upgrade head`) before the first worker comes up.

### B.3 Common failure modes & where to look

| Symptom | First check |
|---|---|
| Tasks stuck in RUNNING | `reap_stale_tasks` is running? (Beat process alive?) |
| Fan-out always returns `low_trust_score` | Proxy provider down? Probe `instagram.com/robots.txt` from the host |
| Browser launches but never reaches feed | Cookies expired → run `/orchestrator/accounts/{id}/validate` |
| All tasks for an account fail with `OperationalError` | A sibling task is stuck — check `Task.status='RUNNING'` for that `account_id` |
| FFmpeg uniqueize hangs | `ffprobe` on the input bitrate path; check the per-variant timeout |
| Account in `checkpoint_required` | Operator must resolve manually in IG, then `PATCH` the account back to a clean status |
