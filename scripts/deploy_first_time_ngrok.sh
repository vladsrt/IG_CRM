#!/usr/bin/env bash
# First-time deploy in NGROK MODE (no domain, no Caddy, no port 80/443).
#
# Same job as deploy_first_time.sh but uses docker-compose.prod.ngrok.yml.
# Use this when the host network doesn't forward 80/443 (typical for VPS
# inside Proxmox/corporate NAT setups).
#
# Order of operations:
#   1. (optional) sudo bash scripts/init_env.sh
#   2. sudo bash scripts/setup_ngrok.sh      # installs ngrok, claims domain
#   3. sudo bash scripts/deploy_first_time_ngrok.sh    <-- you are here

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [[ $EUID -ne 0 ]]; then
    echo "!! must run as root (sudo bash ...)" >&2; exit 1
fi

# ── 1. Docker ─────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "==> Installing Docker (official convenience script)..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    echo "==> Docker already installed: $(docker --version)"
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "!! 'docker compose' plugin missing" >&2; exit 1
fi

# ── 2. .env sanity ────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    echo "!! .env not found. Run init_env.sh first." >&2; exit 1
fi
if grep -q "__REPLACE_ME__" .env; then
    echo "!! .env still has __REPLACE_ME__ placeholders." >&2
    grep -n "__REPLACE_ME__" .env >&2 || true
    exit 1
fi

# ── 3. Build & up (ngrok compose file) ────────────────────────────────────
COMPOSE="docker compose -f docker-compose.prod.ngrok.yml"

echo "==> Building backend image (one-time, ~5-7 min)..."
$COMPOSE build

echo "==> Bringing up postgres + redis + api/worker/beat (no caddy)..."
$COMPOSE up -d

echo "==> Waiting 8s for services to settle..."
sleep 8

$COMPOSE ps

# ── 4. Health check via the ngrok tunnel (if ngrok service is running) ───
DOMAIN_VAL="$(grep '^DOMAIN=' .env | cut -d= -f2)"

echo
if systemctl is-active --quiet ngrok 2>/dev/null; then
    echo "==> Hitting https://${DOMAIN_VAL}/health via ngrok tunnel..."
    if curl -fsS --max-time 15 "https://${DOMAIN_VAL}/health"; then
        echo
        echo
        echo "✓ Backend reachable at https://${DOMAIN_VAL}"
    else
        echo
        echo "!! /health didn't respond yet. Common reasons:"
        echo "   - api container still starting → wait 30s and retry curl"
        echo "   - ngrok not yet connected → check: systemctl status ngrok"
        echo "   - logs:  $COMPOSE logs --tail=80 api"
    fi
else
    echo "!! ngrok systemd service is NOT running."
    echo "   Run: sudo bash scripts/setup_ngrok.sh"
fi

echo
echo "==> Next steps:"
echo "    - All logs: $COMPOSE logs -f"
echo "    - ngrok log: tail -f /var/log/ngrok.log"
echo "    - Update:  bash scripts/update.sh"
echo "    - Frontend: deploy to Vercel with NEXT_PUBLIC_API_URL=https://${DOMAIN_VAL}"
