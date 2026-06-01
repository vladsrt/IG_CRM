#!/usr/bin/env bash
# Install ngrok on the server and set it up as a systemd service so it
# tunnels 127.0.0.1:8000 (the api container) to a public HTTPS URL
# automatically, restarting on reboot.
#
# Run on the VM as root, AFTER `bash scripts/init_env.sh` (we read .env).
#
# Usage:
#   sudo bash scripts/setup_ngrok.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $EUID -ne 0 ]]; then
    echo "!! must run as root" >&2
    exit 1
fi
if [[ ! -f .env ]]; then
    echo "!! .env not found. Run scripts/init_env.sh first." >&2
    exit 1
fi

# ── ask the two ngrok-specific values that .env doesn't have yet ────────
echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Need two values from your ngrok dashboard (free tier)."
echo "  https://dashboard.ngrok.com/"
echo "═══════════════════════════════════════════════════════════════════"
echo
echo "1) Your auth token: https://dashboard.ngrok.com/get-started/your-authtoken"
read -rp "  paste authtoken: " NGROK_TOKEN
NGROK_TOKEN="${NGROK_TOKEN// /}"
if [[ -z "$NGROK_TOKEN" ]]; then
    echo "!! empty token, aborting" >&2; exit 1
fi

echo
echo "2) Your static domain: https://dashboard.ngrok.com/domains"
echo "   Free tier gives you ONE static domain (e.g. 'tasty-bird-42.ngrok-free.app')."
echo "   Claim one if you haven't yet — 'New Domain' button on that page."
read -rp "  paste your static domain (without https://): " NGROK_DOMAIN
NGROK_DOMAIN="${NGROK_DOMAIN// /}"
NGROK_DOMAIN="${NGROK_DOMAIN#https://}"
NGROK_DOMAIN="${NGROK_DOMAIN#http://}"
NGROK_DOMAIN="${NGROK_DOMAIN%/}"
if [[ -z "$NGROK_DOMAIN" || "$NGROK_DOMAIN" != *"."* ]]; then
    echo "!! bad domain, aborting" >&2; exit 1
fi

# ── install ngrok if missing ────────────────────────────────────────────
if ! command -v ngrok >/dev/null 2>&1; then
    echo "==> Installing ngrok via official apt repo..."
    curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | gpg --dearmor -o /etc/apt/keyrings/ngrok.gpg
    echo "deb [signed-by=/etc/apt/keyrings/ngrok.gpg] https://ngrok-agent.s3.amazonaws.com buster main" \
        > /etc/apt/sources.list.d/ngrok.list
    apt-get update -qq
    apt-get install -y ngrok
fi
echo "==> ngrok: $(ngrok --version)"

# ── write ngrok config (system-wide, owned by root for the systemd unit) ─
mkdir -p /etc/ngrok
cat > /etc/ngrok/ngrok.yml <<EOF
version: 3
agent:
  authtoken: $NGROK_TOKEN
endpoints:
  - name: ig_crm_api
    url: https://$NGROK_DOMAIN
    upstream:
      url: 8000
EOF
chmod 600 /etc/ngrok/ngrok.yml
echo "==> wrote /etc/ngrok/ngrok.yml"

# ── persist DOMAIN in .env so the app's logs/CORS know it ───────────────
# also writes NGROK_DOMAIN for any other tool that wants the raw value.
if grep -q '^DOMAIN=' .env; then
    sed -i "s|^DOMAIN=.*|DOMAIN=$NGROK_DOMAIN|" .env
else
    echo "DOMAIN=$NGROK_DOMAIN" >> .env
fi
echo "==> updated DOMAIN in .env -> $NGROK_DOMAIN"

# ── systemd unit so ngrok restarts on reboot / crash ────────────────────
cat > /etc/systemd/system/ngrok.service <<'EOF'
[Unit]
Description=ngrok tunnel for ig_crm_api (port 8000 -> public https)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
# Run as root because /etc/ngrok/ngrok.yml has chmod 600. Acceptable here
# because ngrok itself is a single trusted binary.
ExecStart=/usr/bin/ngrok start --all --config=/etc/ngrok/ngrok.yml --log=stdout --log-format=logfmt
Restart=always
RestartSec=5
StandardOutput=append:/var/log/ngrok.log
StandardError=append:/var/log/ngrok.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ngrok.service
systemctl restart ngrok.service
sleep 2

echo
echo "==> ngrok service status:"
systemctl --no-pager status ngrok.service | head -15 || true
echo
echo "==> Live ngrok log (last 20 lines):"
tail -n 20 /var/log/ngrok.log 2>/dev/null || echo "(no log yet)"

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  ✓ ngrok is running. Public URL: https://$NGROK_DOMAIN"
echo
echo "  Next step (if you haven't already): bring up the docker stack"
echo "    sudo bash scripts/deploy_first_time_ngrok.sh"
echo
echo "  Health check (once the stack is up):"
echo "    curl https://$NGROK_DOMAIN/health"
echo "═══════════════════════════════════════════════════════════════════"
