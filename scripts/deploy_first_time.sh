#!/usr/bin/env bash
# First-time deploy on a fresh Linux VPS.
#
# Tested on: Ubuntu 22.04 / 24.04, Debian 12. Should work on any systemd-based
# distro with apt. Run AS ROOT (or with sudo).
#
# What this does:
#   1. Installs docker + compose plugin if missing.
#   2. Opens the firewall for HTTP/HTTPS.
#   3. Validates that .env exists and has the must-fill placeholders replaced.
#   4. Builds the backend image and brings the stack up.
#   5. Tails logs so you can watch the first migration + start succeed.
#
# What this DOES NOT do:
#   - Configure DNS — you must point your domain at this server FIRST.
#   - Generate SECRET_KEY for you — see the .env file for the openssl command.
#   - Deploy the frontend — that goes to Vercel separately.
#
# Usage:
#   curl -fsSL https://your-repo/raw/main/scripts/deploy_first_time.sh | sudo bash
#   # OR: git clone <repo>; cd repo; sudo bash scripts/deploy_first_time.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [[ $EUID -ne 0 ]]; then
    echo "!! must run as root (sudo bash scripts/deploy_first_time.sh)" >&2
    exit 1
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
    echo "!! 'docker compose' plugin missing — install docker-compose-plugin via apt" >&2
    exit 1
fi

# ── 2. Firewall ───────────────────────────────────────────────────────────
if command -v ufw >/dev/null 2>&1; then
    echo "==> Opening 22/80/443 in ufw..."
    ufw allow 22/tcp || true
    ufw allow 80/tcp || true
    ufw allow 443/tcp || true
    ufw --force enable || true
elif command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload
else
    echo "==> No ufw/firewall-cmd found — assuming firewall is managed elsewhere."
fi

# ── 3. .env sanity ────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
    echo "!! .env not found. Copy the template and fill it in first:" >&2
    echo "     cp .env.production.example .env" >&2
    echo "     nano .env" >&2
    exit 1
fi
if grep -q "__REPLACE_ME__" .env; then
    echo "!! .env still has __REPLACE_ME__ placeholders. Fill them in first." >&2
    grep -n "__REPLACE_ME__" .env >&2 || true
    exit 1
fi
if ! grep -qE "^DOMAIN=[^[:space:]]+\." .env; then
    echo "!! DOMAIN= in .env doesn't look like a real domain (need at least one dot)." >&2
    exit 1
fi

# ── 4. Build & up ─────────────────────────────────────────────────────────
echo "==> Building backend image (one-time, ~5 min)..."
docker compose -f docker-compose.prod.yml build

echo "==> Bringing stack up (postgres → migrate → api/worker/beat → caddy)..."
docker compose -f docker-compose.prod.yml up -d

echo "==> Waiting 8s for services to settle..."
sleep 8

docker compose -f docker-compose.prod.yml ps

# ── 5. Self-check ─────────────────────────────────────────────────────────
echo "==> Hitting /health through caddy on localhost..."
DOMAIN_VAL="$(grep '^DOMAIN=' .env | cut -d= -f2)"
if curl -fsS --max-time 10 "https://${DOMAIN_VAL}/health" 2>&1; then
    echo
    echo "==> ✓ Backend reachable at https://${DOMAIN_VAL}/health"
else
    echo
    echo "!! /health did not respond yet. Possible reasons:"
    echo "   - DNS hasn't propagated → check 'dig ${DOMAIN_VAL}' from another box"
    echo "   - Caddy still provisioning SSL cert (takes 30-90s on first launch)"
    echo "   - Run: docker compose -f docker-compose.prod.yml logs caddy api"
fi

echo
echo "==> Next steps:"
echo "    - Logs:    docker compose -f docker-compose.prod.yml logs -f"
echo "    - Stop:    docker compose -f docker-compose.prod.yml down"
echo "    - Update:  git pull && docker compose -f docker-compose.prod.yml up -d --build"
echo "    - Frontend: deploy to Vercel with NEXT_PUBLIC_API_URL=https://${DOMAIN_VAL}"
