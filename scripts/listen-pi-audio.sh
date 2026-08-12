#!/usr/bin/env bash
# Stream the Pi's Pulse monitor (what leaves the AUX jack / HDMI) to this machine.
# Usage:
#   ./scripts/listen-pi-audio.sh [pi_host]
# Then press Play in the dashboard (or start any playback on the Pi).
#
# Must be run from Linux/WSL, not from PowerShell: it needs a local PulseAudio
# player. Under WSL that is provided by WSLg (/mnt/wslg/PulseServer).
#
# WHAT THIS IS NOT: a timing reference. This is a fixed-rate PCM stream with no
# clock recovery -- nothing in the chain drops samples when the local player
# falls behind, so any network hiccup or drift between the Pi's sound clock and
# this machine's is absorbed as extra delay that is never given back. The audio
# stays correct but drifts steadily further behind real time, and it has been
# observed tens of seconds late after a long session. Never judge how quickly
# the Pi reacted to a command (volume, seek, play) by what you hear here; the
# latency flags below only keep the *starting* delay small.
set -euo pipefail

HOST="${1:-pi@100.84.184.1}"
RATE="${RATE:-44100}"
# Keep both ends on small buffers so the stream starts close to real time.
CAPTURE_LATENCY_MS="${CAPTURE_LATENCY_MS:-50}"
PLAY_LATENCY_MS="${PLAY_LATENCY_MS:-100}"

if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh not found. Run this from WSL/Linux, not PowerShell." >&2
  exit 1
fi

# Pick a local player. paplay is preferred; ffplay works without PulseAudio.
if command -v paplay >/dev/null 2>&1; then
  PLAYER=(paplay --raw --format=s16le --rate="${RATE}" --channels=2
          --latency-msec="${PLAY_LATENCY_MS}")
elif command -v ffplay >/dev/null 2>&1; then
  PLAYER=(ffplay -loglevel quiet -nodisp -autoexit -fflags nobuffer -flags low_delay
          -probesize 32 -analyzeduration 0 -f s16le -ar "${RATE}" -ac 2 -i -)
elif command -v aplay >/dev/null 2>&1; then
  PLAYER=(aplay -q -f S16_LE -r "${RATE}" -c 2 -B "$((PLAY_LATENCY_MS * 1000))")
else
  cat >&2 <<'EOF'
ERROR: no local audio player found (need paplay, ffplay or aplay).
On WSL/Debian/Ubuntu install one of:
    sudo apt install pulseaudio-utils     # provides paplay
    sudo apt install ffmpeg               # provides ffplay
Under WSL also make sure WSLg audio exists: ls -l /mnt/wslg/PulseServer
EOF
  exit 1
fi

# Resolve the monitor source on the Pi instead of hardcoding one card's name, so this
# keeps working on a Pi that outputs over HDMI or has a different sound card.
echo "Resolving audio monitor on ${HOST}..."
SINK_MONITOR="${SINK_MONITOR:-}"
if [[ -z "${SINK_MONITOR}" ]]; then
  SINK_MONITOR="$(ssh -o StrictHostKeyChecking=accept-new "${HOST}" \
    "export XDG_RUNTIME_DIR=/run/user/\$(id -u); pactl get-default-sink 2>/dev/null" | tr -d '\r')"
  if [[ -n "${SINK_MONITOR}" ]]; then
    SINK_MONITOR="${SINK_MONITOR}.monitor"
  fi
fi
if [[ -z "${SINK_MONITOR}" ]]; then
  echo "ERROR: could not determine the Pi's default sink. Is PulseAudio running there?" >&2
  echo "       Check: ssh ${HOST} 'XDG_RUNTIME_DIR=/run/user/1000 pactl info'" >&2
  exit 1
fi

echo "Monitor: ${SINK_MONITOR}"
echo "Player:  ${PLAYER[0]}"
echo "Listening to ${HOST} -> local speakers (Ctrl+C to stop)"
echo "Start/resume playback on the Pi while this runs."
echo
echo "NOTE: this stream drifts behind real time the longer it runs, so it tells"
echo "      you WHAT the Pi is playing, never WHEN it reacted. If a volume or"
echo "      seek change seems late here, restart this script before suspecting"
echo "      the Pi."

# Compression only adds latency and CPU on already-compressed audio.
ssh -o StrictHostKeyChecking=accept-new -o Compression=no "${HOST}" \
  "export XDG_RUNTIME_DIR=/run/user/\$(id -u); exec parec -d '${SINK_MONITOR}' --format=s16le --rate=${RATE} --channels=2 --latency-msec=${CAPTURE_LATENCY_MS}" \
  | "${PLAYER[@]}"
