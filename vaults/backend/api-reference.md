---
title: API Reference
created: 2026-05-12
updated: 2026-05-13
tags: [api, rest, frontend, endpoints, metrics]
---

# API Reference

Cheat-sheet for frontend developers. All endpoints return JSON unless noted.

---

## Auth (`/auth`)

| Method | Path | Body | Returns | Auth? |
|--------|------|------|---------|-------|
| `POST` | `/auth/register` | `{ email, password }` | `UserRead` (201) | No |
| `POST` | `/auth/login` | `OAuth2PasswordRequestForm` | `{ access_token, token_type }` | No |

---

## Users (`/users`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Create user + free subscription |
| `GET` | `/` | List all users |
| `GET` | `/{user_id}` | Get one user |
| `PATCH` | `/{user_id}` | Update email or password |
| `DELETE` | `/{user_id}` | Delete user and all data |
| `GET` | `/{user_id}/subscription` | Get subscription status |
| `PATCH` | `/{user_id}/subscription` | Change subscription tier |

---

## Instagram Accounts (`/accounts`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Add an IG account |
| `GET` | `/` | List accounts (filter by `user_id`, `tags`) |
| `GET` | `/{account_id}` | Get one account |
| `PATCH` | `/{account_id}` | Update credentials, tags, proxy |
| `DELETE` | `/{account_id}` | Remove account |

---

## Proxies (`/proxies`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Add a proxy |
| `GET` | `/` | List all proxies |
| `GET` | `/{proxy_id}` | Get one proxy |
| `PATCH` | `/{proxy_id}` | Update credentials or rotation URL |
| `DELETE` | `/{proxy_id}` | Delete proxy |

---

## Tasks (`/tasks`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/` | Create a task manually |
| `GET` | `/` | List tasks (poll for status updates) |
| `GET` | `/{task_id}` | Get task details + status |
| `PATCH` | `/{task_id}` | Update task |
| `DELETE` | `/{task_id}` | Delete task |

**Status flow**: `pending` → `running` → `completed` / `failed`

---

## Media (`/media`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/folders` | Create media folder |
| `GET` | `/folders` | List folders |
| `DELETE` | `/folders/{folder_id}` | Delete folder |
| `GET` | `/assets` | List assets |
| `GET` | `/assets/{asset_id}` | Get asset details |
| `POST` | `/assets/upload` | Upload file (`multipart/form-data`) |
| `POST` | `/assets/{asset_id}/uniqueize` | Trigger FFmpeg fingerprinting |

---

## Orchestrator (`/orchestrator`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/parse-command` | Parse natural language → task plan |
| `POST` | `/tasks/fan-out` | Distribute plan to target accounts |
| `POST` | `/proxy-probe` | Manual proxy health check |

### Fan-out response shape

```json
{
  "dispatched_count": 3,
  "skipped_count": 1,
  "dispatched": [
    { "account_id": "...", "task_id": "...", "celery_task_id": "..." }
  ],
  "skipped": [
    { "account_id": "...", "reason": "low_trust_score (score=40/100)" }
  ],
  "celery_task_ids": ["...", "..."]
}
```

---

## AI (`/ai`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/parse` | AI entity/action extraction |

---

## Metrics (`/metrics`, `/accounts/{id}/metrics`)

All metrics endpoints require `Authorization: Bearer <token>`. Data is scoped to the authenticated user's accounts only.

| Method | Path | Description | Auth? |
|--------|------|-------------|-------|
| `GET` | `/metrics/dashboard` | Aggregated totals across all user's accounts | Yes |
| `GET` | `/accounts/{account_id}/metrics` | Time-series data for one account (last 30 days) | Yes |

### Dashboard response shape

```json
{
  "total_accounts": 5,
  "total_followers": 48200,
  "total_reel_views": 1250000,
  "total_tasks_running": 2
}
```

### Time-series response shape

```json
{
  "account_id": "550e8400-...",
  "metrics": [
    {
      "id": "...",
      "account_id": "550e8400-...",
      "metric_type": "followers",
      "value": 12500,
      "reel_pk": null,
      "captured_at": "2026-05-13T14:00:00Z"
    }
  ]
}
```

---

## Notes for frontend

1. **Authentication**: Send `Authorization: Bearer <token>` header on protected routes.
2. **Task polling**: Short-poll `GET /tasks/{task_id}` every 2-3 seconds to update UI.
3. **Trust gate**: Fan-out silently skips low-trust accounts — check `skipped` array.
4. **Checkpoints**: If `task.error_log` contains "checkpoint", show an alert to the user.
5. **Metrics dashboard**: Poll `GET /metrics/dashboard` every 30-60 seconds for live totals. Data is collected automatically every hour by the stats worker.
6. **Charts**: Use `GET /accounts/{id}/metrics` for the last 30 days of time-series data. Group by `metric_type` to plot followers, reach, and reel views on separate axes.
