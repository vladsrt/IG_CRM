# IG CRM — Frontend API Documentation

> Version 0.7.0 — covers all routers mounted in `app.api.main:app`
> Base URL: `http://localhost:8000` (OpenAPI docs at `/docs`)

---

## Authentication

All endpoints except `/health`, `/auth/register`, and `/auth/login` require
a JWT Bearer token.  Include the header:

```
Authorization: Bearer <access_token>
```

### `POST /auth/register`
Register a new user.  Auto-provisions a free subscription.

**Request** `application/json`
```json
{
  "email": "user@example.com",
  "password": "secret1234"
}
```
Password minimum 8 chars.

**Response** `201 Created`
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "created_at": "2026-05-17T12:00:00Z"
}
```
Errors: `409` if email already taken.

### `POST /auth/login`
Sign in and receive a JWT.

**Request** `application/x-www-form-urlencoded`
```
username=user@example.com
password=secret1234
```

**Response** `200`
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer"
}
```
Errors: `401` wrong credentials.

---

## Users

Prefix: `/users`

### `POST /`
Same as `/auth/register` — create user + free subscription.

### `GET /`
List all users (paginated).

**Query params:** `skip` (default 0), `limit` (1–500, default 100)

### `GET /{user_id}`
Get a single user by UUID.

### `PATCH /{user_id}`
Update user fields (email, etc.).

### `DELETE /{user_id}`
Remove user. Returns `204 No Content`.

### `GET /{user_id}/subscription`
Read the user's billing subscription.

**Response**
```json
{
  "id": "uuid",
  "user_id": "uuid",
  "tier": "free",
  "valid_until": "2026-06-17T12:00:00Z"
}
```
Tiers: `free`, `pro`, `enterprise`.

### `PATCH /{user_id}/subscription`
Upgrade/downgrade subscription tier.

**Request**
```json
{
  "tier": "pro",
  "valid_until": "2026-12-31T00:00:00Z"
}
```

---

## Instagram Accounts

Prefix: `/accounts`

### `POST /`
Connect a new Instagram account.

**Request**
```json
{
  "ig_username": "myaccount",
  "ig_password": "igpass123",
  "auth_method": "password",
  "user_id": "uuid",
  "proxy_id": null,
  "proxy_session_id": null,
  "cookies": [],
  "tags": ["crypto", "fashion"],
  "platform": "windows",
  "user_agent": null
}
```
`auth_method` enum: `password`, `cookie_only`
`platform` enum: `windows`, `macos`, `linux`

### `GET /`
List accounts. Supports filtering by `user_id` query param.

### `GET /{account_id}`
Single account detail, includes `last_check` timestamp and `error_log`.

**Response**
```json
{
  "id": "uuid",
  "user_id": "uuid",
  "ig_username": "myaccount",
  "auth_method": "password",
  "status": "active",
  "tags": ["crypto"],
  "platform": "windows",
  "user_agent": "Mozilla/5.0 ...",
  "last_check": "2026-05-16T10:00:00Z",
  "error_log": null
}
```

### `PATCH /{account_id}`
Partial update. Send only fields you want to change.
Useful for: updating `status` (e.g. `checkpoint_required`), changing proxy, editing tags.

### `DELETE /{account_id}`
Remove account. `204 No Content`.

---

## Proxies

Prefix: `/proxies`

### `POST /`
Add a proxy server.

**Request**
```json
{
  "host": "1.2.3.4",
  "port": 8080,
  "username": "proxyuser",
  "password": "proxypass",
  "protocol": "http",
  "rotation_url": null
}
```
`protocol` enum: `http`, `https`, `socks4`, `socks5`

### `GET /`
List proxies (paginated: `skip`, `limit`).

### `GET /{proxy_id}`
Single proxy detail.

### `PATCH /{proxy_id}`
Update proxy fields.

### `DELETE /{proxy_id}`
Remove proxy. `204 No Content`.

---

## Tasks

Prefix: `/tasks`

### `POST /`
Create a task manually (debug/direct path).

**Request**
```json
{
  "account_id": "uuid",
  "user_id": "uuid",
  "payload": {
    "commands": [
      {"action": "warmup", "args": {"duration_minutes": 15}}
    ]
  },
  "priority": 5
}
```

### `GET /`
List all tasks. Paginated.
Filter by `status` query param: `pending`, `running`, `completed`, `failed`, `draft`.

### `GET /{task_id}`
Single task detail including payload, status, error_log, timestamps.

**Response**
```json
{
  "id": "uuid",
  "account_id": "uuid",
  "status": "completed",
  "payload": {...},
  "error_log": null,
  "celery_task_id": "uuid",
  "created_at": "...",
  "started_at": null,
  "completed_at": "..."
}
```

### `PATCH /{task_id}`
Update task status or payload. Useful for manual retry (reset to `pending`).

### `DELETE /{task_id}`
Remove task. `204 No Content`.

---

## Orchestrator

Prefix: `/orchestrator`

This is the main entry point for running actions at scale.

### `POST /accounts/{account_id}/validate`
Queue a cookie validation check for one account.
Returns `202 Accepted` with the Celery task id.

```json
{
  "celery_task_id": "uuid",
  "account_id": "uuid"
}
```

### `POST /tasks/{task_id}/start`
Dispatch a single pre-created task to a Celery worker.
Returns `202 Accepted`.

### `POST /tasks/fan-out`
The core dispatch endpoint. Takes an LLM-generated plan + explicit account IDs,
runs trust-score gating and spintax per account, creates one Task row per account,
and queues them on Celery.

**Request**
```json
{
  "plan": {
    "summary": "Post the reel to all crypto accounts",
    "priority": "high",
    "target_tags": ["crypto"],
    "clarification_needed": null,
    "commands": [
      {
        "action": "upload_reels",
        "args": {
          "file_path": "/media/videos/my_reel.mp4",
          "caption": "Check out this {cool|awesome} reel!"
        }
      }
    ]
  },
  "target_account_ids": []
}
```

**Response** `202 Accepted`
```json
{
  "requested_accounts": 12,
  "dispatched_count": 10,
  "skipped_count": 2,
  "dispatched": [
    {
      "task_id": "uuid",
      "account_id": "uuid",
      "celery_task_id": "uuid"
    }
  ],
  "skipped": [
    {
      "account_id": "uuid",
      "reason": "low_trust_score (score=35/100): proxy unreachable"
    }
  ],
  "celery_task_ids": ["uuid", "uuid", ...]
}
```

`clarification_needed` — if not null in the plan, the frontend MUST prompt the user
before calling fan-out.  The endpoint will refuse if clarification is needed.

`celery_task_ids` is a flat list you can use to poll `/tasks/{id}` for progress.

---

## AI

Prefix: `/ai`

### `POST /generate-task`
Convert a natural-language instruction into a structured task plan.
**Side-effect free** — no database writes, no Celery dispatch.
Call this first, show the plan to the user, then POST to `/orchestrator/tasks/fan-out`.

**Request**
```json
{
  "user_id": "uuid",
  "prompt": "warm up my crypto accounts, then post the new reel everywhere"
}
```

**Response** `200`
```json
{
  "summary": "Warm up crypto accounts, then upload the reel",
  "priority": "high",
  "target_tags": ["crypto"],
  "clarification_needed": null,
  "commands": [
    {"action": "warmup", "args": {"duration_minutes": 15}},
    {"action": "upload_reels", "args": {"file_path": "...", "caption": "..."}}
  ]
}
```

When `clarification_needed` is not null — show it to the user and ask them
to refine the prompt.  Example:

```json
{
  "summary": null,
  "priority": "low",
  "target_tags": [],
  "clarification_needed": "Which accounts do you want to post to?",
  "commands": []
}
```

---

## Media

Prefix: `/media`

### Folders

`POST /media/folders` — create folder
```json
{"name": "Reels Jan 2026", "user_id": "uuid"}
```

`GET /media/folders` — list folders (paginated)

`DELETE /media/folders/{folder_id}` — remove folder

### Assets

`GET /media/assets` — list assets (paginated)

`GET /media/assets/{asset_id}` — single asset detail

### `POST /media/upload`
Upload a media file.  Max 500 MB.

**Request** `multipart/form-data`
| Field | Type | Description |
|---|---|---|
| `file` | file (binary) | The media file to upload |
| `user_id` | string (UUID) | Owner user |
| `folder_id` | string (UUID) | Optional — target folder |
| `tags` | string (comma-sep) | Optional — e.g. "reel,draft" |

**Response** `201`
```json
{
  "id": "uuid",
  "original_filename": "my_reel.mp4",
  "size_bytes": 12345678,
  "mime_type": "video/mp4",
  "status": "raw",
  "folder_id": null,
  "metadata": {},
  "created_at": "2026-05-17T12:00:00Z"
}
```

### `POST /media/{asset_id}/uniqueize?copies=N`
Queue FFmpeg processing to create N uniqueized clones.
Adds bitrate jitter, noise filter, and strips metadata per clone.

**Response** `202`
```json
{
  "celery_task_id": "uuid",
  "asset_id": "uuid",
  "copies": 5
}
```

---

## Metrics

Prefix: `/metrics`

### `GET /metrics/dashboard`
Aggregated metrics across all accounts.  Use for operator overview.

**Query params:** `user_id` (required), `days` (default 7)

**Response**
```json
{
  "user_id": "uuid",
  "window_days": 7,
  "accounts_total": 12,
  "accounts_active": 10,
  "accounts_checkpoint": 1,
  "total_followers": 45600,
  "avg_trust_score": 82.5,
  "tasks_completed_24h": 45,
  "tasks_failed_24h": 3
}
```

### `GET /metrics/account/{account_id}`
Time-series metrics for a single account.
Most recent 30 days of data, grouped by `metric_type`.

**Response**
```json
[
  {
    "metric_type": "follower_count",
    "value": 1234,
    "captured_at": "2026-05-17T12:00:00Z"
  },
  {
    "metric_type": "reach",
    "value": 890,
    "captured_at": "2026-05-17T12:00:00Z"
  }
]
```

Metric types include:
- `follower_count`
- `following_count`
- `post_count`
- `reach` (from network interception)
- `reel_play_count`
- `reel_view_count`

---

## Common Patterns

### Flow: prompt → plan → approve → fan-out

```
1. POST /ai/generate-task        → user reviews plan
2. POST /orchestrator/tasks/fan-out  → tasks dispatched
3. GET /tasks?status=running     → poll for progress
4. GET /tasks/{task_id}          → check specific task
```

### Flow: upload media → uniqueize → post

```
1. POST /media/upload            → asset saved as "raw"
2. POST /media/{asset_id}/uniqueize?copies=5  → FFmpeg processing
3. GET /media/assets/{asset_id}  → check status: "processing" → "ready"
4. Use asset path in AI prompt or fan-out plan
```

### Task status lifecycle

```
DRAFT → PENDING → RUNNING → COMPLETED
                  ↓
                FAILED  (handler crash, checkpoint, timeout, reaped)
```

### Error responses (all endpoints)

```json
{
  "detail": "Human-readable error message"
}
```

HTTP codes: `400` bad request, `401` unauthorized, `404` not found,
`409` conflict, `422` validation error.

### Pagination

Endpoints supporting pagination accept query params:
- `skip` — offset (default 0, minimum 0)
- `limit` — page size (default 100, range 1–500)

---

## Status Codes Summary

| Code | Meaning |
|---|---|
| 200 | Success (GET, PATCH, login) |
| 201 | Created (POST) |
| 202 | Accepted — queued, not finished yet |
| 204 | No Content (DELETE) |
| 400 | Bad Request — invalid input |
| 401 | Unauthorized — missing/invalid JWT |
| 404 | Not Found |
| 409 | Conflict — duplicate email, etc. |
| 422 | Validation Error — malformed JSON body |
