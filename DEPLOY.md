# IG_CRM — Production deploy guide

This is the only doc you need. Read it once top-to-bottom, then run the
commands. Total first-time deploy time: ~20 min once DNS is set.

---

## 0. Architecture (what you're deploying)

```
                     ┌─ Vercel ──────────────┐
   browser ─HTTPS──→ │ Next.js frontend       │ ─XHR─▶  https://api.yourdomain.com
                     └────────────────────────┘                │
                                                               ▼
                                  ┌────────────────────────────────────┐
                                  │  Your Linux VPS                    │
                                  │  ┌──────────────────────────────┐  │
                                  │  │ Caddy   :80 :443 (auto-SSL)  │  │
                                  │  └────────────┬─────────────────┘  │
                                  │               │ reverse_proxy      │
                                  │  ┌────────────▼─────────────────┐  │
                                  │  │ FastAPI (uvicorn)            │  │
                                  │  └─┬─────┬───────────────────┬──┘  │
                                  │    │     │                   │     │
                                  │  ┌─▼──┐ ┌▼────────┐  ┌───────▼──┐  │
                                  │  │PG  │ │Redis    │  │Celery     │ │
                                  │  └────┘ └─────────┘  │ worker x N│ │
                                  │                      │ + beat    │ │
                                  │                      │ + Chrome  │ │
                                  │                      └───────────┘ │
                                  └────────────────────────────────────┘
```

Everything on the right-hand box is one `docker compose` stack.

---

## 1. Prerequisites

Buy/prepare these BEFORE running any commands:

1. **VPS**: 4 vCPU / 8 GB RAM / 40 GB disk minimum.
   Tested on Hetzner CPX31, Vultr 8GB, DigitalOcean 8GB Premium.
   Ubuntu 22.04 or 24.04 — anything older = newer Docker is a pain.

2. **Domain**: e.g. `api.yourdomain.com` (subdomain is fine).
   In your DNS panel add an **A record**:
   ```
   api.yourdomain.com  →  <server's public IP>
   ```
   Wait for it to resolve (`dig api.yourdomain.com` from your laptop should
   show the server IP) before continuing.

3. **OpenAI API key** (or compatible — Ollama Cloud, z.ai etc).

4. **SSH access** to the server with sudo.

---

## 2. First-time deploy

```bash
# On the server (as root, or with sudo):
git clone https://github.com/YourOrg/IG_CRM.git
cd IG_CRM

# Copy the env template and fill it in
cp .env.production.example .env
nano .env
#   Required edits:
#     DOMAIN=api.yourdomain.com
#     SECRET_KEY=$(openssl rand -hex 32)        # paste the output
#     POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d /=+)
#     DATABASE_URL=postgresql+psycopg://ig_user:<SAME PASSWORD>@postgres:5432/ig_crm
#     OPENAI_API_KEY=sk-proj-...
#     ADMIN_EMAILS=you@yourdomain.com
#     CORS_ALLOWED_ORIGINS=["https://app.yourdomain.com"]

# Run the deploy script
sudo bash scripts/deploy_first_time.sh
```

The script:
1. Installs Docker if missing.
2. Opens firewall ports 22/80/443.
3. Validates that `.env` is filled (no `__REPLACE_ME__` left).
4. Builds the backend image (~5 min — Chrome + ffmpeg + Python deps).
5. Starts the stack and waits for Postgres health.
6. Applies all migrations (one-shot `migrate` service).
7. Hits `https://<DOMAIN>/health` to confirm Caddy got an SSL cert.

If the final health check is green, the backend is live.

---

## 3. Frontend (Vercel)

The Next.js frontend deploys to Vercel separately. It's just a static client.

```bash
# From your laptop:
cd frontend/front_IG/instagram-crm-architecture
npx vercel              # first time: links the project
npx vercel --prod       # builds + deploys to production
```

In **Vercel project settings → Environment Variables** set:
```
NEXT_PUBLIC_API_URL = https://api.yourdomain.com
```

After redeploying once with that variable, the frontend will talk to your VPS.

> ⚠️ `NEXT_PUBLIC_API_URL` is **baked at build time** — changing it later
> requires another `vercel --prod` to take effect.

---

## 4. First user + admin grant

The API auto-creates accounts on `POST /auth/register`. Whoever first
registers with an email matching `ADMIN_EMAILS` in `.env` gets the admin
role automatically.

```bash
# From your laptop OR the server:
curl -X POST https://api.yourdomain.com/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@yourdomain.com","password":"PUT-A-STRONG-PASSWORD-HERE"}'
```

Then log in on the frontend. The **Admin** sidebar item appears for admin
emails only.

---

## 5. Daily operations

| Action | Command |
|---|---|
| View live logs (all services) | `docker compose -f docker-compose.prod.yml logs -f` |
| View live logs (one service)  | `docker compose -f docker-compose.prod.yml logs -f api` |
| Restart everything | `docker compose -f docker-compose.prod.yml restart` |
| Stop everything | `docker compose -f docker-compose.prod.yml down` |
| Update to latest commit | `bash scripts/update.sh` |
| Backup the DB | `sudo bash scripts/backup_db.sh` |
| Cleanup local junk | `bash scripts/cleanup.sh` |

---

## 6. Where to find things

| Thing | Location |
|---|---|
| Live logs (host filesystem) | `docker volume inspect ig_crm_logs` → mountpoint contains `api.log`, `worker.log`, `beat.log` (rotated 20 MB × 5) |
| Postgres data | docker volume `ig_crm_pgdata` |
| Uploaded media | docker volume `ig_crm_media` |
| Caddy access log | docker volume `ig_crm_caddydata` → `/data/access.log` |
| DB backups (after `backup_db.sh`) | `/var/backups/ig_crm/` |

To `cd` into a volume on Ubuntu:
```bash
sudo ls /var/lib/docker/volumes/ig_crm_logs/_data
sudo tail -f /var/lib/docker/volumes/ig_crm_logs/_data/worker.log
```

---

## 7. Scaling

The default config is sized for **2 parallel agents** per host. To bump:

1. Edit `.env`:
   ```
   WORKER_CONCURRENCY=4         # ↑ more parallel browsers
   CAPACITY_MAX_BROWSERS=4      # ↑ same or higher
   ```
2. Per-user limit (lets a paid plan run more): admin grants via the UI or:
   ```bash
   docker compose -f docker-compose.prod.yml exec api \
     uv run --no-sync python -m app.scripts.swap_admin --help
   ```
3. Restart workers: `docker compose -f docker-compose.prod.yml restart worker`.

RAM cost: ~500 MB per browser + 50 MB per pproxy auth-forwarder. On 8 GB
box, 4 agents leaves you ~1.5 GB headroom for postgres/redis/caddy.

---

## 8. Troubleshooting

### Caddy can't get SSL cert
Check `docker compose -f docker-compose.prod.yml logs caddy`. Usual causes:
- DNS not propagated yet (`dig <domain>` should return your server's IP).
- Port 80 not reachable from the internet (firewall, cloud provider rule).

### Worker keeps restarting / Chrome crashes
- `shm_size: 2gb` is already set in compose. If you're on a tiny VPS with
  little RAM, drop `WORKER_CONCURRENCY` to 1 in `.env`.
- Check `docker compose ... logs worker` for the actual exception.

### Migrations fail on startup
- The `migrate` service runs once and exits. If it failed:
  ```bash
  docker compose -f docker-compose.prod.yml logs migrate
  ```
- Fix the cause (usually a model/schema mismatch), then:
  ```bash
  docker compose -f docker-compose.prod.yml up -d --force-recreate migrate
  ```

### "Proxy verify FAILED: browser IP equals home IP"
The local pproxy forwarder is bypassed. Check `worker.log` for the
upstream proxy's actual response. Usually it's:
- Wrong creds in the per-account proxy config (UI → Accounts → Edit).
- Upstream proxy IP-whitelist doesn't include this server's IP.

### Tasks stuck in RUNNING forever after a worker crash
Reap runs every 60 s with a 15 min threshold. Or kill them manually:
```bash
docker compose -f docker-compose.prod.yml exec api \
  uv run --no-sync python -c "
from app.core.database import SessionLocal
from app.models.task import Task
with SessionLocal() as db:
    n = db.query(Task).filter(Task.status=='running').update({'status':'failed','error_log':'manual reset'})
    db.commit()
    print(f'reset {n}')
"
```

---

## 9. Backups (cron)

Add to root's crontab on the server:
```bash
sudo crontab -e
```
```
# Daily DB dump at 03:30 server time, keep 7 days
30 3 * * *  /full/path/to/IG_CRM/scripts/backup_db.sh /var/backups/ig_crm >> /var/log/ig_crm_backup.log 2>&1
```

For off-site backup, rsync `/var/backups/ig_crm` to S3 / another box. Don't
skip this — Postgres data + Fernet-encrypted cookies are not replaceable.

---

## 10. Security checklist before going public

- [ ] `SECRET_KEY` was generated with `openssl rand -hex 32` and is NOT the
      one in `.env.production.example`.
- [ ] `POSTGRES_PASSWORD` is long and random.
- [ ] `CORS_ALLOWED_ORIGINS` is your real frontend origin, **not** `["*"]`.
- [ ] `OPENAI_API_KEY` is the **new** key — the one previously committed
      to local `.env` during development was leaked into chat history and
      MUST be rotated at platform.openai.com first.
- [ ] `ALLOW_NO_PROXY=false` (the default).
- [ ] `MIN_TRUST_SCORE=50` once your proxy pool is reliable.
- [ ] Firewall: 22/tcp restricted to your IP if possible, only 80/443 fully open.
- [ ] DB backups configured (section 9).
- [ ] You've tested `bash scripts/update.sh` once so you know how to deploy a fix.
