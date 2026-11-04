#!/bin/bash
# Wi-Fi from the card: soundops-wifi.txt on the boot partition (the drive Windows
# shows after flashing). If it names a network, it becomes a NetworkManager
# connection and the file is reset, so the password does not stay on the FAT
# partition. Runs on every boot: put the card in a PC again to change networks.
# Ethernet stays preferred (NetworkManager's lower route metric).
set -u

FILE=/boot/firmware/soundops-wifi.txt
CON=soundops-wifi
[ -f "$FILE" ] || exit 0

# Notepad may add a BOM and CRLF; values may be quoted
value() {
  sed -e '1s/^\xEF\xBB\xBF//' -e 's/\r$//' "$FILE" \
    | grep -m1 -E "^[[:space:]]*$1[[:space:]]*=" \
    | sed -E 's/^[^=]*=//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"(.*)"$/\1/'
}

ssid="$(value WIFI_SSID)"
password="$(value WIFI_PASSWORD)"
hidden="$(value WIFI_HIDDEN | tr 'A-Z' 'a-z')"
[ -n "$ssid" ] || exit 0

rfkill unblock wifi 2>/dev/null || true
nmcli radio wifi on 2>/dev/null || true

reset_file() {
  # ENVIRON, not -v: an SSID with a backslash stays as it is
  STATUS="# $1" awk '$0 == "# STATUS" { print ENVIRON["STATUS"]; next } { print }' \
    /usr/local/lib/soundops/soundops-wifi.txt >"$FILE"
  sync
}

if [ -n "$password" ] && { [ "${#password}" -lt 8 ] || [ "${#password}" -gt 63 ]; }; then
  reset_file "שגיאה $(date '+%d/%m/%Y %H:%M'): הסיסמה של \"$ssid\" צריכה להיות 8 עד 63 תווים. לא נשמר כלום."
  echo "soundops-wifi: password for $ssid has a bad length, ignored"
  exit 0
fi

nmcli connection delete "$CON" >/dev/null 2>&1 || true
args=(connection add type wifi ifname wlan0 con-name "$CON" ssid "$ssid"
      connection.autoconnect yes connection.autoconnect-priority 5)
[ -n "$password" ] && args+=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$password")
[ "$hidden" = "yes" ] && args+=(802-11-wireless.hidden yes)

if nmcli "${args[@]}" >/dev/null; then
  reset_file "הרשת \"$ssid\" נשמרה בנגן ב-$(date '+%d/%m/%Y %H:%M'). הסיסמה נמחקה מהקובץ."
  echo "soundops-wifi: saved network $ssid"
  nmcli connection up "$CON" >/dev/null 2>&1 &
else
  reset_file "שגיאה $(date '+%d/%m/%Y %H:%M'): לא הצלחתי לשמור את הרשת \"$ssid\"."
  echo "soundops-wifi: nmcli failed for $ssid"
fi
exit 0
