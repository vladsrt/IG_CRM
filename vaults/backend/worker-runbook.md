---
title: Worker Runbook
created: 2026-05-13
updated: 2026-05-13
tags: [celery, workers, ops, concurrency, postgres, locking]
purpose: operational guide for running and scaling Celery workers
---

# Worker Runbook

How to run, monitor, and scale the Celery worker fleet. Covers queue topology, concurrency safety, and the dedicated stats worker.

---

## Queue Topology

The system uses two Celery queues to isolate heavy browser automation from lightweight metrics collection.

| Queue | Name | Purpose | Concurrency |
|-------|------|---------|-------------|
| Default | `ig_crm.default` | Upload, warmup, profile edits — all standard IG automation | 1–4 per host |
| Stats | `stats_queue` | Headless stats collection via `gather_account_metrics` | 1–2 per host |

Separating queues prevents a slow video upload from blocking metrics collection, and vice versa. A dying stats worker never affects the upload pipeline.

---

## Launch Commands

### Default worker (standard automation)

```bash
celery -A app.core.celery_app.celery_app worker \
    -Q ig_crm.default \
    --loglevel=info \
    --concurrency=2 \
    --hostname=worker-default@%h
```

### Stats worker (metrics collection)

```bash
celery -A app.core.celery_app.celery_app worker \
    -Q stats_queue \
    --loglevel=info \
    --concurrency=1 \
    --hostname=worker-stats@%h
```

> **Why concurrency=1?** Each stats task spawns a headless Chromium instance. Running more than 1–2 concurrent browsers on a single host exhausts RAM fast. Start with 1 and scale horizontally by adding hosts, not threads.

### Beat scheduler (periodic dispatch)

```bash
celery -A app.core.celery_app.celery_app beat \
    --loglevel=info
```

Beat runs two scheduled jobs:

| Job | Interval | Target Queue |
|-----|----------|-------------|
| `reap-stale-tasks-every-5-min` | 5 min | default |
| `dispatch-stats-collection-every-1h` | 1 hour | stats_queue |

### All-in-one development

For local development, you can start everything in one terminal:

```bash
celery -A app.core.celery_app.celery_app worker \
    -Q ig_crm.default,stats_queue \
    --loglevel=info \
    --concurrency=2 \
    --hostname=worker-dev@%h \
    -B
```

The `-B` flag embeds Beat in the worker. Never use this in production — it runs duplicate schedulers if you have multiple workers.

---

## Concurrency Strategy

### The Problem

Instagram flags concurrent sessions on the same account as a compromised login. If a warmup task and a stats collection task both open a browser for `@account_x` at the same time, Meta will ban the account instantly.

### The Solution: `FOR UPDATE NOWAIT`

Every task that opens a browser **must acquire a Postgres row-level lock** on the `instagram_accounts` row before proceeding.

```sql
SELECT * FROM instagram_accounts
WHERE id = '<account_uuid>'
FOR UPDATE NOWAIT;
```

This is a **pessimistic lock**. PostgreSQL guarantees:

- If the row is free, the lock is acquired immediately and held until `COMMIT`.
- If another transaction already holds the lock, `NOWAIT` causes an immediate `OperationalError` instead of waiting.

### How standard tasks use the lock

`run_instagram_task` in `celery_tasks.py`:

1. Opens a DB transaction.
2. `SELECT ... FOR UPDATE NOWAIT` on the account row.
3. If `OperationalError` → another worker is driving this account. Retry with exponential backoff.
4. Checks for sibling tasks in `RUNNING` status as a second safety net.
5. Sets the Task status to `RUNNING` inside the locked transaction.
6. Commits → lock released → browser session begins.

### How the stats worker uses the lock

`gather_account_metrics` uses the exact same lock, but with a critical difference in behavior on contention:

1. Opens a DB transaction.
2. `SELECT ... FOR UPDATE NOWAIT` on the account row.
3. If `OperationalError` → **gracefully returns** with `"status": "account_busy"`. No retry, no backoff.
4. If lock acquired → runs the fast `gather_stats` action (~30 seconds).
5. Commits → lock released.

The rationale: stats collection is best-effort. If the account is busy with a real task (upload, warmup), collecting stats can wait for the next hourly Beat cycle. There is zero urgency — we never want stats collection to compete with or delay real automation.

### Sequence diagram

```
Beat (hourly)
  │
  ├─ dispatch_stats_collection
  │   │
  │   ├─ gather_account_metrics(@acc1)  ──► FOR UPDATE NOWAIT ──► ✅ lock free ──► run gather_stats ──► commit
  │   │
  │   ├─ gather_account_metrics(@acc2)  ──► FOR UPDATE NOWAIT ──► ❌ lock held by warmup task
  │   │                                                              │
  │   │                                                              └─► log "Account busy, skipping" ──► return
  │   │
  │   └─ gather_account_metrics(@acc3)  ──► FOR UPDATE NOWAIT ──► ✅ lock free ──► run gather_stats ──► commit
```

### Pre-flight proxy check

Before acquiring the lock, `gather_account_metrics` pings the account's proxy via the trust service's `_evaluate_proxy`. If the proxy is dead, the task aborts immediately without wasting a Chromium launch. This saves ~5 seconds per dead-proxy account.

---

## Stats Collection Flow

The `gather_stats` action is intentionally minimal:

1. Navigate to the account's profile page.
2. Click the Reels tab.
3. Perform 2–3 `micro_scroll()` passes to trigger Instagram's GraphQL pagination.
4. Close the browser.

**No DOM parsing happens.** The `ObservabilityMonitor` (running as a daemon thread inside `TaskExecutor`) intercepts all network traffic matching IG's GraphQL endpoints. When Instagram returns JSON with `follower_count`, `play_count`, or `reach`, the monitor buffers the values. After the browser closes, `TaskExecutor._persist_and_analyze()` flushes them into the `account_metrics` table.

This network-level approach means we never depend on IG's DOM structure for stats. The page layout can change completely and stats collection still works, as long as the API response shape stays the same.

---

## Monitoring

### Celery Flower (optional)

```bash
celery -A app.core.celery_app.celery_app flower --port=5555
```

Check `stats_queue` depth and task success rate at `http://localhost:5555`.

### Key log lines to watch

| Log message | Meaning |
|-------------|---------|
| `[gather_account_metrics] account_id=... account busy, skipping` | Lock contention — normal, try next hour |
| `[gather_account_metrics] proxy dead for account_id=...` | Proxy needs replacement |
| `[observability] captured followers=...` | Stats intercepted successfully |
| `[metrics] persisted N sample(s)` | Data saved to `account_metrics` |
| `[reap_stale_tasks] reaped N stuck Task(s)` | Dead workers cleaned up |

---

## Scaling Notes

- **Horizontal scaling**: add more hosts running the `stats_queue` worker. The `FOR UPDATE NOWAIT` lock guarantees two workers never process the same account.
- **Vertical scaling**: increase `--concurrency` cautiously. Each Chromium instance uses ~200–400 MB RAM even in headless mode.
- **Beat**: must run as a **single instance** across the entire cluster. Use `celery-beat-redis` or a similar lock-backed scheduler in production to prevent duplicate dispatches.
