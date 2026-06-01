#!/usr/bin/env bash
# Postgres → gzipped dump on the host. Run from cron or by hand.
#
# Usage:
#   sudo bash scripts/backup_db.sh [output-dir]
#
# Default output dir: /var/backups/ig_crm (created if missing).
# Keeps the last 7 daily backups, deletes older ones.

set -euo pipefail

OUT_DIR="${1:-/var/backups/ig_crm}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$OUT_DIR/ig_crm_${STAMP}.sql.gz"

echo "==> Dumping to $OUT ..."
docker compose -f "$(dirname "$0")/../docker-compose.prod.yml" exec -T postgres \
    pg_dump -U "${POSTGRES_USER:-ig_user}" -d "${POSTGRES_DB:-ig_crm}" \
    | gzip > "$OUT"

echo "==> Rotating: keep last 7 .sql.gz, delete older."
ls -1t "$OUT_DIR"/ig_crm_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "==> Done. Current backups:"
ls -lh "$OUT_DIR"/ig_crm_*.sql.gz 2>/dev/null || echo "(none yet)"
