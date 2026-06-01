#!/usr/bin/env bash
# Hot-update on a running server: pull new code, rebuild, restart.
# Migrations run automatically (the `migrate` one-shot service).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Pulling latest code..."
git pull --ff-only

echo "==> Rebuilding backend image..."
docker compose -f docker-compose.prod.yml build api

echo "==> Restarting api / worker / beat (migrate runs automatically first)..."
docker compose -f docker-compose.prod.yml up -d

echo "==> Tailing api log for 10s to confirm startup..."
timeout 10s docker compose -f docker-compose.prod.yml logs --tail=50 api || true

echo "==> Done. ps:"
docker compose -f docker-compose.prod.yml ps
