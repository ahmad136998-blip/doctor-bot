#!/usr/bin/env bash
# Installs the bot as an always-on systemd service (Ubuntu/Debian, Python 3.10+).
# Usage: sudo bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

[ "$(id -u)" = 0 ] || { echo "Run with: sudo bash install.sh"; exit 1; }

if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
  apt-get update -qq && apt-get install -y python3 python3-venv
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  || { echo "Python 3.10 or newer is required."; exit 1; }

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  read -rsp "BOT_TOKEN (from @BotFather, hidden while typing): " TOKEN; echo
  read -rp "ALLOWED_USER_IDS (leave empty if you don't know yet): " IDS
  printf 'BOT_TOKEN=%s\nALLOWED_USER_IDS=%s\nDATA_DIR=%s/data\n' "$TOKEN" "$IDS" "$DIR" > .env
fi
chmod 600 .env

cat > /etc/systemd/system/doctor-bot.service <<EOF
[Unit]
Description=Doctor patient-files Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable doctor-bot >/dev/null 2>&1
systemctl restart doctor-bot
sleep 3
systemctl --no-pager --lines=5 status doctor-bot || true
echo
echo "Done. Edit $DIR/.env to change settings, then: sudo systemctl restart doctor-bot"
