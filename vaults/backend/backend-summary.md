---
title: Backend Summary
created: 2026-05-12
updated: 2026-05-13
tags: [backend, drissionpage, celery, postgres, automation, metrics]
---

# Backend — Feature Summary

Everything the backend can do as of **2026-Q2**, grouped by area.

---

## Browser Automation (DrissionPage)

### Coordinate-based clicking
Every click in the system goes through `HumanBehaviorEngine`. Instead of calling `.click()` on a DOM element (which fires a synthetic JS event that Instagram can fingerprint), we:

1. Read the element's bounding box via `element.rect.midpoint`.
2. Move the mouse along a Bézier curve with randomized control points.
3. Fire a **hardware-level** click at the computed `(x, y)` coordinates.

This makes our clicks indistinguishable from real user input at the browser event level.

### SVG → clickable parent resolution
Instagram's UI is full of SVG icons wrapped in `<a>`, `<button>`, or `[role="button"]` parents. Clicking the SVG itself throws `TypeError` because SVGs don't have a `.click()` method in the DOM. The `_walk_up_to_clickable` function walks up the DOM tree looking for the nearest interactive ancestor:

- `@role=button` → `tag:button` → `tag:a` → `parent(2)` → `parent(1)`

### 3-layer modal dismissal
Instagram throws modals constantly ("Turn on notifications", "Save login info", cookie consent). Our `dismiss_instagram_modals` function handles them with three fallback layers:

- **L1** — Find and click text buttons: "Not Now", "Cancel", "OK", "Close", etc.
- **L2** — Click the backdrop at viewport coordinates `(10, 10)` to dismiss overlay modals.
- **L3** — Send ESC key via three methods: raw `\x1b`, W3C key `\ue00c`, and a JS-dispatched `KeyboardEvent`.

### Human behavior simulation
`HumanBehaviorEngine` adds realistic delays and movements:

- **Smooth scrolling** with variable speed and overshoot.
- **Hover-then-click** — mouse hovers over an element before clicking, with a random dwell time.
- **Read pauses** — delays proportional to content length, simulating actual reading.
- **Idle periods** — random pauses between actions.
- **Typing** — character-by-character input with variable inter-key delays.

---

## Actions

### Warmup 2.0
Time-bounded session that keeps accounts looking alive. Features:

- **Weighted random action selection**: scroll feed, watch reels, visit profiles, open comments.
- **Hardcoded engagement probabilities**: 30% like, 45% open comments, 60-70% per-comment like.
- **Session loop**: runs until `time.monotonic() < end_time` (default 15 min).
- **Feed recovery**: if the browser drifts to the wrong URL, it auto-navigates back.

### Upload (Post/Reel)
Full upload flow through Instagram's web Create dialog:

- Uses **CDP file chooser interception** to inject files into the hidden `<input type="file">`.
- Navigates via left-rail "Create" icon with hover-hydration.
- Handles caption typing, aspect ratio selection, and the Share button.
- 3-layer modal sweep runs at the start to clear any blocking overlays.

### Update Profile
Three code paths for editing:

- **Bio only** — types into the bio textarea, clicks Submit, waits for "Profile saved." toast.
- **Avatar only** — injects file via hidden input, waits for "Profile photo added." toast, closes dialog. No Submit needed (async upload).
- **Bio + Avatar** — combined: avatar dialog is cancelled first, then bio + Submit fires.

### Privacy toggle
Navigates to `/accounts/who_can_see_your_content/`, reads `aria-checked` on the toggle, clicks only if the state needs to change, and handles the confirmation modal.

### Gather Stats (technical worker)
Lightweight, headless-only action for the dedicated stats worker. Opens the account profile, navigates to the Reels tab, and performs 2–3 micro-scrolls to trigger Instagram's GraphQL pagination. No DOM parsing — the `ObservabilityMonitor` intercepts the network responses directly and saves follower count, reach, and reel view counts to `account_metrics`. Expected runtime: ~30 seconds per account.

---

## Celery Orchestration

### Fan-out dispatch
The orchestrator receives a **plan** (parsed from natural language by the AI) and fans it out to multiple accounts:

1. Resolves target accounts by tags or explicit IDs.
2. Runs a **trust evaluation** on each account (proxy health, user-agent validity, hygiene tags).
3. Skips accounts that score below threshold (default: 50/100).
4. Creates a `Task` row per account and dispatches `run_instagram_task.delay(task_id)`.

### Pessimistic row locking
`run_instagram_task` uses `SELECT ... FOR UPDATE NOWAIT` on the account row to prevent two workers from running actions on the same Instagram account simultaneously. If the lock is held, the task retries with exponential backoff via Celery's `self.retry()`.

### Dedicated stats worker
The `gather_account_metrics` task runs on a separate `stats_queue` to avoid blocking standard automation. It uses the same `FOR UPDATE NOWAIT` lock but with a critical difference: on contention, it **skips** the account instead of retrying, since metrics can wait for the next hourly cycle. See [[worker-runbook]] for the full concurrency strategy.

### Orphan recovery
If a task arrives in `RUNNING` status (meaning the previous worker crashed mid-execution), it's immediately marked `FAILED` with an "Orphan recovery" error log instead of re-executing.

---

## Trust Scoring

Weighted score out of 100:

| Component | Weight | Passes when |
|-----------|--------|-------------|
| Proxy | 60 | Proxy is attached and responds to HTTP HEAD |
| User-Agent | 25 | Matches a known Chrome UA pattern, < 512 chars |
| Hygiene | 15 | No risk tags (`possible_shadowban`, etc.), no `checkpoint_required` status |

Accounts scoring below 50 are **skipped** during fan-out, not failed.

---

## Path Traversal Protection

`resolve_within_media_root` is the single security gate between user-controlled file paths and the filesystem. It rejects:

- Absolute paths outside `MEDIA_ROOT`
- Relative `../` traversals
- Symlinks resolving outside the root
- Non-existent paths, directories, empty strings
