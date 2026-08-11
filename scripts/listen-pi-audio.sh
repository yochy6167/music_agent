#!/usr/bin/env bash
# Stream the Pi's Pulse monitor (what leaves the AUX jack) to this machine.
# Usage:
#   ./scripts/listen-pi-audio.sh [pi_host]
# Then press Play in the dashboard (or start any playback on the Pi).
set -euo pipefail

HOST="${1:-pi@100.84.184.1}"
SINK_MONITOR="${SINK_MONITOR:-alsa_output.platform-fe00b840.mailbox.stereo-fallback.monitor}"
RATE="${RATE:-44100}"

echo "Listening to ${HOST} monitor → local speakers (Ctrl+C to stop)"
echo "Start/resume playback on the Pi while this runs."

ssh -o StrictHostKeyChecking=accept-new "${HOST}" \
  "export XDG_RUNTIME_DIR=/run/user/1000; exec parec -d '${SINK_MONITOR}' --format=s16le --rate=${RATE} --channels=2" \
  | paplay --raw --format=s16le --rate="${RATE}" --channels=2
