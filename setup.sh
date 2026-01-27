#!/usr/bin/env bash
set -e
sed -i 's/\r$//' "$0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Updating apt packages..."
sudo apt-get update

echo "Installing system dependencies..."
sudo apt-get install -y python3 python3-venv python3-pip vlc libvlc-dev

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
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_BIN} ${SCRIPT_DIR}/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "Adding user to audio and video groups..."
sudo usermod -aG audio,video "${USER}"

echo "Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo "Setup complete."
echo "Service '${SERVICE_NAME}' is running and enabled on boot."
echo "If audio/video groups were updated, log out and back in to apply."
