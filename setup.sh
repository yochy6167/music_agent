#!/usr/bin/env bash
set -euo pipefail
sed -i 's/\r$//' "$0" 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DNS_SERVERS="${DNS_SERVERS:-1.1.1.1 8.8.8.8}"
SET_DNS="${SET_DNS:-0}"
SKIP_DNS="${SKIP_DNS:-0}"

maybe_configure_dns() {
  if [[ "${SKIP_DNS}" == "1" ]]; then
    echo "Skipping DNS configuration (SKIP_DNS=1)."
    return 0
  fi
  if [[ "${SET_DNS}" != "1" ]]; then
    echo "DNS configuration not requested (SET_DNS=1 to enable)."
    return 0
  fi
  if ! command -v nmcli >/dev/null 2>&1; then
    echo "nmcli not found; skipping DNS configuration."
    return 0
  fi

  local conn_name
  conn_name="$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | awk -F: '$2 != "" && $2 != "lo" { print $1; exit }')"
  if [[ -z "${conn_name}" ]]; then
    echo "No active NetworkManager connection found; skipping DNS configuration."
    return 0
  fi

  echo "Configuring DNS for connection '${conn_name}' to: ${DNS_SERVERS}"
  sudo nmcli connection modify "${conn_name}" ipv4.ignore-auto-dns yes ipv4.dns "${DNS_SERVERS}"
  sudo nmcli connection up "${conn_name}" >/dev/null 2>&1 || true
}

if [[ ! -f config.json ]]; then
  if [[ -f config.json.example ]]; then
    cp config.json.example config.json
    echo "Created config.json from config.json.example"
  else
    echo "ERROR: config.json missing and no config.json.example found."
    exit 1
  fi
fi

maybe_configure_dns

echo "Updating apt packages..."
sudo apt-get update -qq

echo "Installing system dependencies..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip \
  vlc libvlc-dev \
  git curl \
  alsa-utils

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "Configuring systemd service..."
SERVICE_NAME="music_agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python3"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Neeman Music Agent
After=network-online.target sound.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${USER}
WorkingDirectory=${SCRIPT_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/main.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

echo "Adding user to audio and video groups..."
sudo usermod -aG audio,video "${USER}"

echo "Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo "Running quick health checks..."
python3 -V
${PYTHON_BIN} -c "import httpx, websockets; print('python deps: ok')"
curl -fsS "$(python3 -c "import json; print(json.load(open('config.json'))['api_url'].rstrip('/') + '/health' )" 2>/dev/null)" >/dev/null 2>&1 || true

echo "Setup complete."
echo "Service '${SERVICE_NAME}' is running and enabled on boot."
echo "If audio/video groups were updated, log out and back in to apply."
