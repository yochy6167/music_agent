#!/bin/bash
# SoundOps first boot: this Pi's own identity. The image has none, so every card
# flashed from it gets its own hostname, SSH host keys and claim_secret here
# (machine-id comes from systemd, the disk is grown by Raspberry Pi OS itself).
# Runs once: soundops-firstboot.service needs /etc/soundops/provisioned missing.
set -u

SYS=/etc/soundops
install -d -m 0755 "$SYS"

# The id the server knows the player by: the Serial line of /proc/cpuinfo, as
# main.py get_rpi_serial() reads it. Fallbacks only for boards without one.
serial="$(awk -F': *' 'tolower($1) ~ /^serial/ { print $2; exit }' /proc/cpuinfo | tr -d '[:space:]')"
if [ -z "$serial" ] && [ -r /sys/firmware/devicetree/base/serial-number ]; then
  serial="$(tr -d '\0[:space:]' </sys/firmware/devicetree/base/serial-number)"
fi
if [ -z "$serial" ]; then
  [ -s "$SYS/fallback_id" ] || tr -d '-' </proc/sys/kernel/random/uuid >"$SYS/fallback_id"
  serial="$(cat "$SYS/fallback_id")"
fi

suffix="$(printf '%s' "$serial" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | tail -c 6)"
host="soundops-${suffix:-new}"
printf '%s\n' "$host" >/etc/hostname
hostname "$host"
sed -i '/^127\.0\.1\.1[[:space:]]/d' /etc/hosts
printf '127.0.1.1\t%s\n' "$host" >>/etc/hosts

# SSH host keys of this Pi (only the missing ones; Raspberry Pi OS may have made them)
ssh-keygen -A >/dev/null 2>&1 || true

# The secret the agent shows when it registers: after approval, the token goes
# only to whoever has it (a copied serial is not enough)
if [ ! -s "$SYS/claim_secret" ]; then
  (umask 077; head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' >"$SYS/claim_secret")
fi
chown pi:pi "$SYS/claim_secret"
chmod 0600 "$SYS/claim_secret"

image_version="$(tr -d '\r\n' </boot/firmware/soundops-image-version 2>/dev/null || echo unknown)"
cat >"$SYS/device.json" <<EOF
{"hardware_id": "$serial", "hostname": "$host", "image_version": "$image_version", "first_boot_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
chmod 0644 "$SYS/device.json"

rfkill unblock all 2>/dev/null || true

touch "$SYS/provisioned"
echo "SoundOps first boot done: $host ($serial), image $image_version"
