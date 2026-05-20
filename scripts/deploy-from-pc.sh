#!/usr/bin/env bash
# Deploy music_agent to a Raspberry Pi over SSH and run setup.sh (optionally with DNS fix).
# Usage:
#   ./scripts/deploy-from-pc.sh [--set-dns] [--dns "1.1.1.1 8.8.8.8"] user@host
# Examples:
#   ./scripts/deploy-from-pc.sh pi@music-agent-01.local
#   ./scripts/deploy-from-pc.sh --set-dns pi@music-agent-01.local
set -euo pipefail

SET_DNS=0
DNS_SERVERS="1.1.1.1 8.8.8.8"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --set-dns)
      SET_DNS=1
      shift
      ;;
    --dns)
      DNS_SERVERS="${2:-}"
      if [[ -z "${DNS_SERVERS}" ]]; then
        echo "ERROR: --dns requires a value, e.g. --dns \"1.1.1.1 8.8.8.8\""
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--set-dns] [--dns \"1.1.1.1 8.8.8.8\"] user@host"
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 [--set-dns] [--dns \"1.1.1.1 8.8.8.8\"] user@hostname"
  echo "Example: $0 --set-dns pi@music-agent-01.local"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REMOTE_DIR="music_agent"

echo "==> Testing SSH to ${TARGET}..."
ssh -o ConnectTimeout=10 -o BatchMode=yes "${TARGET}" "echo OK" || {
  echo "SSH failed. Try: ssh ${TARGET}"
  exit 1
}

echo "==> Syncing project to ~/${REMOTE_DIR} on Pi..."
ssh "${TARGET}" "mkdir -p ~/${REMOTE_DIR}"

rsync -avz --delete \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude 'config.json' \
  --exclude '.env' \
  --exclude '*.log' \
  "${PROJECT_DIR}/" "${TARGET}:~/${REMOTE_DIR}/"

echo "==> Ensuring config.json exists on Pi..."
ssh "${TARGET}" bash -s <<'REMOTE'
set -euo pipefail
cd ~/music_agent
if [[ ! -f config.json ]]; then
  cp config.json.example config.json
  echo "Created config.json from config.json.example"
else
  echo "Keeping existing config.json (device_token preserved)"
fi
REMOTE

echo "==> Running setup.sh on Pi (may take several minutes)..."
SSH_ENV=""
if [[ "${SET_DNS}" == "1" ]]; then
  SSH_ENV="SET_DNS=1 DNS_SERVERS='${DNS_SERVERS}'"
fi

ssh -t "${TARGET}" "cd ~/${REMOTE_DIR} && sed -i 's/\r$//' setup.sh scripts/*.sh 2>/dev/null || true && ${SSH_ENV} bash setup.sh"

echo ""
echo "==> Deploy complete."
echo "    Logs:  ssh ${TARGET} 'journalctl -u music_agent -f'"
echo "    Status: ssh ${TARGET} 'sudo systemctl status music_agent'"
