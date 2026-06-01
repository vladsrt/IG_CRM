#!/usr/bin/env bash
# Wipe local junk before commit / before zipping for upload.
# Idempotent — safe to run multiple times.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> Removing Python caches..."
find . -type d -name "__pycache__" \
    -not -path "./.venv/*" \
    -not -path "./frontend/*" \
    -prune -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" \
    -not -path "./.venv/*" \
    -not -path "./frontend/*" \
    -delete 2>/dev/null || true

echo "==> Removing pytest / mypy / ruff caches..."
rm -rf .pytest_cache .mypy_cache .ruff_cache

echo "==> Removing Celery beat schedule files..."
rm -f backend/celerybeat-schedule* celerybeat-schedule*

echo "==> Removing orphaned proxy plugins..."
rm -rf runtime_proxy_plugin_* backend/runtime_proxy_plugin_* /tmp/dp_proxy_ext_* 2>/dev/null || true
rm -rf /tmp/DrissionPage/userData 2>/dev/null || true

echo "==> Removing log files (if any leaked into repo)..."
find . -name "*.log" \
    -not -path "./.venv/*" \
    -not -path "./frontend/node_modules/*" \
    -not -path "./frontend/*/.next/*" \
    -delete 2>/dev/null || true

echo "==> Final repo size (excluding ignored):"
git ls-files | xargs du -ch 2>/dev/null | tail -1 || echo "(not a git repo or empty)"

echo "==> Done."
