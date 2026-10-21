#!/usr/bin/env bash
# wifi_connect.sh - scan Wi-Fi networks, pick one, enter password, and save it
# so the Pi reconnects automatically on every boot (NetworkManager / nmcli).
# Usage (from the repo folder, over SSH):   ./wifi_connect.sh
# Options:  ./wifi_connect.sh --list      just list saved Wi-Fi networks
#           ./wifi_connect.sh --forget    delete a saved Wi-Fi network
# Ethernet stays preferred when a cable is plugged in; Wi-Fi takes over without it.
set -uo pipefail
sed -i 's/\r$//' "$0" 2>/dev/null || true

IFACE="${WIFI_IFACE:-wlan0}"
COUNTRY="${WIFI_COUNTRY:-IL}"
DNS_SERVERS="${DNS_SERVERS:-}"   # optional, e.g. DNS_SERVERS="1.1.1.1 8.8.8.8" ./wifi_connect.sh

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

if ! command -v nmcli >/dev/null 2>&1; then
  red "nmcli not found. This script needs NetworkManager (Raspberry Pi OS Bookworm or newer)."
  exit 1
fi
if [[ $EUID -ne 0 ]]; then SUDO="sudo"; else SUDO=""; fi

saved_wifi() {
  nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}'
}

if [[ "${1:-}" == "--list" ]]; then
  bold "Saved Wi-Fi networks:"; saved_wifi | nl -w2 -s') '; exit 0
fi

if [[ "${1:-}" == "--forget" ]]; then
  mapfile -t SAVED < <(saved_wifi)
  if [[ ${#SAVED[@]} -eq 0 ]]; then echo "No saved Wi-Fi networks."; exit 0; fi
  for i in "${!SAVED[@]}"; do printf '%2d) %s\n' "$((i+1))" "${SAVED[$i]}"; done
  read -rp "Number to delete: " n
  if [[ "$n" =~ ^[0-9]+$ ]] && (( n>=1 && n<=${#SAVED[@]} )); then
    $SUDO nmcli connection delete "${SAVED[$((n-1))]}" && green "Deleted."
  else red "Invalid choice."; fi
  exit 0
fi

# --- make sure the Wi-Fi radio is usable ---------------------------------
if command -v raspi-config >/dev/null 2>&1; then
  cur="$($SUDO raspi-config nonint get_wifi_country 2>/dev/null || true)"
  if [[ -z "$cur" ]]; then
    echo "Setting Wi-Fi country to ${COUNTRY} (required before Wi-Fi works)..."
    $SUDO raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 || true
  fi
fi
$SUDO rfkill unblock wifi 2>/dev/null || true
$SUDO nmcli radio wifi on 2>/dev/null || true

if ! nmcli -t -f DEVICE,TYPE device | grep -q "^${IFACE}:wifi$"; then
  red "Wi-Fi interface ${IFACE} not found."; nmcli device status; exit 1
fi

# --- scan ------------------------------------------------------------------
while true; do
  echo; bold "Scanning for Wi-Fi networks..."
  $SUDO nmcli device wifi rescan ifname "$IFACE" >/dev/null 2>&1 || true
  sleep 3
  SSIDS=(); SIGS=(); SECS=()
  declare -A SEEN=()
  # fields: SIGNAL:SECURITY:SSID   (SSID last, ':' inside it is escaped as '\:')
  while IFS= read -r line; do
    sig="${line%%:*}"; rest="${line#*:}"
    sec="${rest%%:*}"; ssid="${rest#*:}"
    ssid="${ssid//\\:/:}"
    [[ -z "$ssid" ]] && continue
    [[ -n "${SEEN[$ssid]:-}" ]] && continue
    SEEN[$ssid]=1
    SSIDS+=("$ssid"); SIGS+=("$sig"); SECS+=("$sec")
  done < <(nmcli -t -f SIGNAL,SECURITY,SSID device wifi list ifname "$IFACE" 2>/dev/null | sort -t: -k1,1nr)
  unset SEEN

  if [[ ${#SSIDS[@]} -eq 0 ]]; then
    red "No networks found."
  else
    printf '\n %-3s %-6s %-12s %s\n' "#" "Signal" "Security" "Network"
    for i in "${!SSIDS[@]}"; do
      s="${SECS[$i]}"; [[ -z "$s" || "$s" == "--" ]] && s="open"
      printf ' %-3s %-6s %-12s %s\n' "$((i+1)))" "${SIGS[$i]}%" "$s" "${SSIDS[$i]}"
    done
  fi
  echo
  echo " r) Rescan    h) Hidden network (type the name)    q) Quit"
  read -rp "Choose a network number: " choice
  case "$choice" in
    r|R) continue ;;
    q|Q) exit 0 ;;
    h|H) read -rp "Network name (SSID): " SSID; HIDDEN=1; SEC="WPA"; break ;;
    *)
      if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice>=1 && choice<=${#SSIDS[@]} )); then
        SSID="${SSIDS[$((choice-1))]}"; SEC="${SECS[$((choice-1))]}"; HIDDEN=0; break
      fi
      red "Invalid choice." ;;
  esac
done

# --- password ----------------------------------------------------------------
PASS=""
if [[ -n "$SEC" && "$SEC" != "--" ]]; then
  while true; do
    read -rsp "Password for \"$SSID\": " PASS; echo
    if (( ${#PASS} >= 8 )); then break; fi
    if [[ "$HIDDEN" == "1" && -z "$PASS" ]]; then break; fi   # hidden open network
    red "Wi-Fi passwords are at least 8 characters. Try again."
  done
fi

# --- save + connect ----------------------------------------------------------
CON="wifi-${SSID}"
$SUDO nmcli connection delete "$CON" >/dev/null 2>&1 || true

echo "Connecting to \"$SSID\"..."
args=(connection add type wifi ifname "$IFACE" con-name "$CON" ssid "$SSID"
      connection.autoconnect yes connection.autoconnect-priority 10
      connection.autoconnect-retries 0 ipv4.route-metric 600)
[[ "$HIDDEN" == "1" ]] && args+=(802-11-wireless.hidden yes)
if [[ -n "$PASS" ]]; then
  KM="wpa-psk"
  [[ "$SEC" == *WPA3* && "$SEC" != *WPA2* && "$SEC" != *WPA1* ]] && KM="sae"   # WPA3-only network
  args+=(wifi-sec.key-mgmt "$KM" wifi-sec.psk "$PASS")
fi
if [[ -n "$DNS_SERVERS" ]]; then
  args+=(ipv4.ignore-auto-dns yes ipv4.dns "$DNS_SERVERS")
fi
if ! $SUDO nmcli "${args[@]}" >/dev/null; then
  red "Could not save the network."; exit 1
fi

if ! $SUDO nmcli --wait 30 connection up "$CON" >/dev/null 2>&1; then
  red "Could not connect. Wrong password or weak signal?"
  read -rp "Keep it saved anyway (it will retry on every boot)? [y/N] " keep
  [[ "$keep" =~ ^[yY]$ ]] || { $SUDO nmcli connection delete "$CON" >/dev/null 2>&1; echo "Removed."; }
  exit 1
fi
unset PASS

IP="$(nmcli -g IP4.ADDRESS device show "$IFACE" 2>/dev/null | head -1)"
green "Connected to \"$SSID\"  (IP: ${IP:-unknown})"
if ping -c 2 -W 3 -I "$IFACE" 8.8.8.8 >/dev/null 2>&1; then
  green "Internet over Wi-Fi: OK"
else
  red "Connected, but no internet over Wi-Fi yet."
fi
echo
echo "Saved: the Pi will join \"$SSID\" automatically on every boot."
echo "If an Ethernet cable is plugged in, Ethernet is used first; Wi-Fi is the backup."
echo "To use Wi-Fi only: unplug the cable. Your SSH address may change to ${IP%/*}."
