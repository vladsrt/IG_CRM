# IG CRM Frontend API Documentation

This document outlines the available REST API endpoints provided by the FastAPI backend for the Instagram CRM application. All endpoints expect and return JSON payloads unless specified otherwise (e.g., file uploads).

## Base URL
All API routes are prefixed with `/api` (or similar depending on your FastAPI setup) and are split by resources.

---

## 1. Authentication & Users (`/users`)
Manage users and their subscription statuses.

* **`POST /`**
  Create a new user.
* **`GET /`**
  List all users.
* **`GET /{user_id}`**
  Get details of a specific user.
* **`PATCH /{user_id}`**
  Update user information.
* **`DELETE /{user_id}`**
  Delete a user.
* **`GET /{user_id}/subscription`**
  Get the subscription status for a user.
* **`PATCH /{user_id}/subscription`**
  Update the user's subscription tier.

---

## 2. Instagram Accounts (`/accounts`)
Manage Instagram accounts connected to the system.

* **`POST /`**
  Add a new Instagram account.
* **`GET /`**
  List Instagram accounts. Can filter by tags or user.
* **`GET /{account_id}`**
  Get details of a specific account.
* **`PATCH /{account_id}`**
  Update account details (e.g., username, password, tags, proxy assignment).
* **`DELETE /{account_id}`**
  Remove an account.

---

## 3. Proxies (`/proxies`)
Manage proxy servers used by the accounts.

* **`POST /`**
  Add a new proxy.
* **`GET /`**
  List all proxies.
* **`GET /{proxy_id}`**
  Get details of a specific proxy.
* **`PATCH /{proxy_id}`**
  Update proxy credentials or rotation URL.
* **`DELETE /{proxy_id}`**
  Delete a proxy.

---

## 4. Tasks (`/tasks`)
Manage tasks that are executed by Celery workers.

* **`POST /`**
  Create a new task manually.
* **`GET /`**
  List tasks. Useful for polling status.
* **`GET /{task_id}`**
  Get details and status of a specific task.
* **`PATCH /{task_id}`**
  Update a task (e.g., changing status or priority).
* **`DELETE /{task_id}`**
  Delete a task.

---

## 5. Media & Assets (`/media`)
Manage media folders and assets for uploads.

* **`POST /folders`**
  Create a new media folder.
* **`GET /folders`**
  List media folders.
* **`DELETE /folders/{folder_id}`**
  Delete a media folder.
* **`GET /assets`**
  List uploaded assets.
* **`GET /assets/{asset_id}`**
  Get details of a specific asset.
* **`POST /assets/upload`**
  Upload a new media file (multipart/form-data).
* **`POST /assets/{asset_id}/uniqueize`**
  Trigger FFmpeg processing to add noise/fingerprinting to an asset.

---

## 6. Orchestrator (`/orchestrator`)
High-level task coordination and fan-out actions.

* **`POST /parse-command`** (or AI parsing route depending on config)
  Parse a natural language prompt into a task plan.
* **`POST /tasks/fan-out`**
  Distribute a plan to multiple target accounts based on tags or direct IDs. It handles checking trust scores and creating Celery tasks for each target.
* **`POST /proxy-probe`**
  Force a manual health check for a proxy.

---

## 7. AI Operations (`/ai`)
Direct interaction with the AI services.

* **`POST /parse`** (or similar, depending on configuration)
  Use AI to parse a prompt and extract entities or actions.

---

### Important Notes for Frontend Developers:
1. **Trust Gate:** When using the Fan-out endpoint, accounts with bad proxies or poor hygiene scores will be silently skipped and returned in a `skipped` list within the response body.
2. **WebSockets/Polling:** For task tracking, you may need to implement short-polling on `/tasks/{task_id}` to update the UI on progress (status changes from `pending` -> `running` -> `completed`/`failed`).
3. **Modals and Verification:** Checkpoint required errors are stored in the task error logs. The frontend should display a clear alert if an account gets flagged.
