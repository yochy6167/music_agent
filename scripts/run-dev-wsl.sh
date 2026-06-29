#!/usr/bin/env bash
# Run the agent in WSL with audio routed to Windows speakers (WSLg).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."

if [[ ! -S /mnt/wslg/PulseServer ]]; then
  echo "WSLg PulseAudio not found. Use Windows 11 + WSL2 with WSLg enabled."
  echo "Test: pactl info"
  exit 1
fi

export PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}"
export PULSE_COOKIE="${PULSE_COOKIE:-/mnt/wslg/PulseCookie}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

/bin/bash scripts/set-system-volume.sh

if [[ ! -d .venv ]]; then
  echo "Missing .venv — run: bash setup.sh"
  exit 1
fi

source .venv/bin/activate
exec python main.py
