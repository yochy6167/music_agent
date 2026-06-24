#!/usr/bin/env bash
# Select PulseAudio/PipeWire output and set volume (runs before each agent start).
# AUDIO_PREFER: auto (default) | hdmi | headphones
set -euo pipefail

VOLUME="${SYSTEM_SINK_VOLUME:-90}"
PREFER="${AUDIO_PREFER:-auto}"

if ! command -v pactl >/dev/null 2>&1; then
  echo "pactl not found; skipping audio output setup."
  exit 0
fi

if [[ -z "${XDG_RUNTIME_DIR:-}" ]] && [[ -d "/run/user/$(id -u)" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi

pick_sink() {
  local sinks="$1"
  local pattern="$2"
  echo "${sinks}" | awk -v pat="${pattern}" '
    $0 ~ pat { print $2; exit }
  '
}

select_sink_name() {
  local sinks
  sinks="$(pactl list sinks short 2>/dev/null || true)"
  if [[ -z "${sinks}" ]]; then
    return 1
  fi

  local chosen=""
  case "${PREFER}" in
    hdmi)
      chosen="$(pick_sink "${sinks}" 'hdmi|HDMI')"
      ;;
    headphones|analog|jack)
      chosen="$(pick_sink "${sinks}" 'Headphones|headphones|analog|Analog|bcm2835')"
      ;;
    auto|*)
      chosen="$(pick_sink "${sinks}" 'hdmi|HDMI')"
      if [[ -z "${chosen}" ]]; then
        chosen="$(pick_sink "${sinks}" 'Headphones|headphones|analog|Analog|bcm2835')"
      fi
      ;;
  esac

  if [[ -z "${chosen}" ]]; then
    chosen="$(echo "${sinks}" | awk 'NR==1 { print $2 }')"
  fi

  if [[ -n "${chosen}" ]]; then
    echo "${chosen}"
    return 0
  fi
  return 1
}

for _ in $(seq 1 30); do
  if pactl info >/dev/null 2>&1; then
    if sink="$(select_sink_name)"; then
      pactl set-default-sink "${sink}"
      pactl set-sink-volume "${sink}" "${VOLUME}%"
      pactl set-sink-mute "${sink}" 0 2>/dev/null || true
      echo "Audio output: ${sink} (${PREFER}, volume ${VOLUME}%)"
    else
      pactl set-sink-volume @DEFAULT_SINK@ "${VOLUME}%"
      pactl set-sink-mute @DEFAULT_SINK@ 0 2>/dev/null || true
      echo "Audio output: @DEFAULT_SINK@ (volume ${VOLUME}%)"
    fi
    exit 0
  fi
  sleep 1
done

echo "WARNING: PulseAudio/PipeWire not ready; could not configure audio output." >&2
exit 0
