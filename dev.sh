#!/usr/bin/env bash
# One-terminal dev launcher for IG CRM.
# Assumes infra (Postgres :5433 + Redis :6379) is already up:
#     sudo docker compose up -d
# Then starts API + Celery worker + Celery beat + frontend together,
# with prefixed combined logs. Ctrl+C stops everything.
#
# No sudo here on purpose — only docker needs sudo, and that's the compose step.

ROOT="/home/sk8ver/Documents/Projects/CRM/IG_CRM"
PY="$ROOT/.venv/bin/python"
FRONT="$ROOT/frontend/front_IG/instagram-crm-architecture"

port_up() { ss -ltn 2>/dev/null | grep -q ":$1\b"; }

echo "==> checking infra"
if ! port_up 5433; then
  echo "!! Postgres not on :5433. Start infra first:  sudo docker compose up -d"
  exit 1
fi
if ! port_up 6379; then
  echo "!! Redis not on :6379. Either it's down, or enable redis in docker-compose.yml."
  exit 1
fi
echo "   postgres :5433 OK, redis :6379 OK"

# don't start a second copy on top of a running one
for p in 8000 3000; do
  if port_up "$p"; then
    echo "!! Port :$p is busy — dev.sh is probably already running."
    echo "   Stop the other one (Ctrl+C in its terminal), or clean up with:"
    echo "     pkill -f 'uvicorn app.api.main'; pkill -f 'celery -A app.core'; fuser -k 3000/tcp"
    exit 1
  fi
done

echo "==> running migrations"
( cd "$ROOT/backend" && "$PY" -m alembic -c ../alembic.ini upgrade head ) || {
  echo "!! migrations failed"; exit 1; }

echo "==> starting api + worker + beat + web (Ctrl+C to stop all)"
# kill the whole process group on exit. `trap - ...` first so this runs ONCE
# (otherwise SIGINT + SIGTERM + EXIT each re-trigger it -> the "stopping" spam).
cleanup() { trap - INT TERM EXIT; echo; echo "==> stopping all..."; kill 0 2>/dev/null; }
trap cleanup INT TERM EXIT

cd "$ROOT/backend"
"$PY" -m uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload 2>&1 \
  | sed -u 's/^/[api]  /' &

"$PY" -m celery -A app.core.celery_app.celery_app worker --loglevel=info \
  -Q ig_crm.default --include=app.workers.celery_tasks,app.workers.media_tasks 2>&1 \
  | sed -u 's/^/[wrk]  /' &

"$PY" -m celery -A app.core.celery_app.celery_app beat --loglevel=info 2>&1 \
  | sed -u 's/^/[beat] /' &

( cd "$FRONT" && ./node_modules/.bin/next dev 2>&1 | sed -u 's/^/[web]  /' ) &

wait
