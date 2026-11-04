#!/bin/bash
# Builds the SoundOps SD image from the official Raspberry Pi OS Lite (arm64).
# Runs in a privileged Debian container (build.ps1 starts it): /src = this
# folder (read-only), /work = a Docker volume (loop mounts), /out = the result.
# Needs arm64 binfmt on the Docker host (build.ps1 registers it).
set -euo pipefail

: "${AGENT_REF:?}" "${IMAGE_VERSION:?}"
AGENT_REPO="${AGENT_REPO:-https://github.com/yochy6167/music_agent.git}"
BASE_URL="${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz}"
BASE_SHA256="${BASE_SHA256:-483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843}"
GROW_MB="${GROW_MB:-3584}"
# Free space left in the image after shrinking (the disk is grown to the card on first boot)
SPARE_MB="${SPARE_MB:-256}"
SSH_PUBKEY="${SSH_PUBKEY:-}"

SRC=/src
WORK=/work
OUT=/out
IMG="$WORK/soundops.img"
ROOT=/mnt/soundops
NAME="soundops-img-v${IMAGE_VERSION}-agent-${AGENT_REF}"
log() { echo "==> $*"; }

log "host tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq xz-utils fdisk mount e2fsprogs zerofree curl ca-certificates >/dev/null 2>&1

cd "$WORK"
rm -f inspect.img
if [ ! -f base.img.xz ] || ! echo "$BASE_SHA256  base.img.xz" | sha256sum -c --quiet; then
  log "download base image"
  curl -fsSL -o base.img.xz "$BASE_URL"
  echo "$BASE_SHA256  base.img.xz" | sha256sum -c --quiet
fi

part() { partx -g -o "$1" -n "$2" "$IMG" | tr -d ' '; }
L1=""; L2=""
attach() {
  # No udev in the container: one loop device per partition, by offset
  L1=$(losetup -f --show -o $((boot_start * 512)) --sizelimit $((boot_sectors * 512)) "$IMG")
  L2=$(losetup -f --show -o $((root_start * 512)) --sizelimit $((root_sectors * 512)) "$IMG")
}
detach() {
  [ -n "$L1" ] && losetup -d "$L1" 2>/dev/null || true
  [ -n "$L2" ] && losetup -d "$L2" 2>/dev/null || true
  L1=""; L2=""
}
cleanup() {
  local m
  for m in dev/pts dev proc sys run tmp boot/firmware ""; do
    if mountpoint -q "$ROOT/$m"; then
      umount "$ROOT/$m" 2>/dev/null || umount -l "$ROOT/$m" || true
    fi
  done
  detach
}
trap cleanup EXIT

build_rootfs() {
log "decompress + grow by ${GROW_MB}MB"
xz -dc base.img.xz >"$IMG"
truncate -s "+${GROW_MB}M" "$IMG"
boot_start=$(part START 1); boot_sectors=$(part SECTORS 1)
root_start=$(part START 2)
echo "${root_start},+" | sfdisk -N 2 --no-reread --no-tell-kernel -q "$IMG"
root_sectors=$(part SECTORS 2)

attach
e2fsck -fy "$L2" >/dev/null || [ $? -le 1 ]
resize2fs "$L2" >/dev/null 2>&1

log "mount"
mkdir -p "$ROOT"
mount "$L2" "$ROOT"
mount "$L1" "$ROOT/boot/firmware"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sys "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"
mount -t tmpfs tmpfs "$ROOT/tmp"

# No service may start inside the chroot; DNS from the container for apt
printf '#!/bin/sh\nexit 101\n' >"$ROOT/usr/sbin/policy-rc.d"
chmod 0755 "$ROOT/usr/sbin/policy-rc.d"
if [ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ]; then
  mv "$ROOT/etc/resolv.conf" "$ROOT/etc/resolv.conf.soundops"
fi
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"

log "SoundOps files"
cp -r --no-preserve=ownership "$SRC/files/." "$ROOT/"
# A Windows checkout may have CRLF line endings
find "$SRC/files" -type f -printf '%P\n' | while read -r f; do sed -i 's/\r$//' "$ROOT/$f"; done
chmod 0755 "$ROOT"/usr/local/lib/soundops/*.sh
chmod 0644 "$ROOT"/etc/systemd/system/soundops-*.service "$ROOT"/etc/systemd/system/music*
chmod 0440 "$ROOT/etc/sudoers.d/010_pi-nopasswd"
awk '$0 == "# STATUS" { print "# עוד לא הוגדרה רשת."; next } { print }' \
  "$ROOT/usr/local/lib/soundops/soundops-wifi.txt" >"$ROOT/boot/firmware/soundops-wifi.txt"
echo "$IMAGE_VERSION" >"$ROOT/boot/firmware/soundops-image-version"
# cloud-init is off: its sample seed files would only mislead
rm -f "$ROOT/boot/firmware/user-data" "$ROOT/boot/firmware/meta-data" "$ROOT/boot/firmware/network-config"

if [ -n "$SSH_PUBKEY" ]; then
  install -d -m 0700 "$ROOT/home/pi/.ssh"
  printf '%s\n' "$SSH_PUBKEY" >"$ROOT/home/pi/.ssh/authorized_keys"
  chmod 0600 "$ROOT/home/pi/.ssh/authorized_keys"
fi

log "rootfs (chroot, arm64): packages, agent, services"
sed 's/\r$//' "$SRC/prepare-rootfs.sh" >"$ROOT/tmp/prepare-rootfs.sh"
chroot "$ROOT" /usr/bin/env -i \
  HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
  AGENT_REF="$AGENT_REF" AGENT_REPO="$AGENT_REPO" IMAGE_VERSION="$IMAGE_VERSION" \
  /bin/bash /tmp/prepare-rootfs.sh

if [ -d /agent ]; then
  # Test build (build.ps1 -LocalAgent): the working copy's code over the tag.
  # The first update from the dashboard stashes it (agent.py _git_checkout).
  log "TEST BUILD: local agent code over ${AGENT_REF}"
  for f in /agent/*.py /agent/requirements.txt; do
    sed 's/\r$//' "$f" >"$ROOT/home/pi/music_agent/$(basename "$f")"
  done
  (cd /agent && find scripts -type f -not -path '*/__pycache__/*' -not -name '*.pyc') | while read -r f; do
    sed 's/\r$//' "/agent/$f" >"$ROOT/home/pi/music_agent/$f"
  done
  chroot "$ROOT" chown -R pi:pi /home/pi/music_agent
  chroot "$ROOT" runuser -u pi -- env HOME=/home/pi /home/pi/music_agent/.venv/bin/python3 -c \
    "import sys; sys.path.insert(0, '/home/pi/music_agent'); import tailscale_ops, device_ops, health" \
    || { echo "  FAIL local agent code does not import"; exit 1; }
fi

log "seal: no identity, no secrets, no caches"
rm -f "$ROOT/usr/sbin/policy-rc.d"
rm -f "$ROOT/etc/resolv.conf"
[ -e "$ROOT/etc/resolv.conf.soundops" ] || [ -L "$ROOT/etc/resolv.conf.soundops" ] && \
  mv "$ROOT/etc/resolv.conf.soundops" "$ROOT/etc/resolv.conf"
chroot "$ROOT" apt-get clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/root/.cache "$ROOT"/home/pi/.cache \
  "$ROOT"/root/.bash_history "$ROOT"/home/pi/.bash_history
rm -f "$ROOT"/etc/ssh/ssh_host_*
rm -rf "$ROOT"/etc/soundops "$ROOT"/var/lib/tailscale/* "$ROOT"/var/lib/bluetooth/* \
  "$ROOT"/home/pi/.config/soundops "$ROOT"/home/pi/.local/state/wireplumber
printf 'uninitialized\n' >"$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id"
find "$ROOT/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' \) -delete
find "$ROOT/var/log" -type f -exec truncate -s 0 {} +
rm -rf "$ROOT"/var/log/journal/*
find "$ROOT/home/pi/music_agent" -name '__pycache__' -type d -prune -exec rm -rf {} +

log "check: nothing secret left (spec P1-8)"
bad=0
jq_devices=$(chroot "$ROOT" jq -c '.devices' /home/pi/music_agent/config.json)
[ "$jq_devices" = "[]" ] || { echo "  FAIL config.json has devices: $jq_devices"; bad=1; }
ls "$ROOT"/etc/ssh/ssh_host_* >/dev/null 2>&1 && { echo "  FAIL SSH host keys left"; bad=1; }
[ -e "$ROOT/etc/soundops" ] && { echo "  FAIL /etc/soundops left"; bad=1; }
[ -n "$(ls -A "$ROOT/var/lib/tailscale" 2>/dev/null)" ] && { echo "  FAIL tailscale state left"; bad=1; }
ls "$ROOT"/etc/NetworkManager/system-connections/* >/dev/null 2>&1 && { echo "  FAIL saved networks left"; bad=1; }
# A real Tailscale key looks like tskey-auth-kXXXX-XXXXXXXXXXXXXXXX (the regex in tailscale_ops.py does not)
grep -rIl --exclude-dir=.venv -E 'tskey-[a-z]+-[A-Za-z0-9]{4,}-[A-Za-z0-9]{12,}|"device_token": *"[^"]' "$ROOT/home/pi" "$ROOT/etc" "$ROOT/boot/firmware" 2>/dev/null \
  && { echo "  FAIL a key or token in the files above"; bad=1; }
[ "$bad" = 0 ] || exit 1
echo "  PASS"
echo "  rootfs $(df -h --output=used "$ROOT" | tail -1 | tr -d ' ') used"
cleanup
sync
}

if [ "${RESUME_SHRINK:-0}" = 1 ] && [ -f "$IMG" ]; then
  # Debugging the last stage: the sealed image the previous run left here
  log "RESUME_SHRINK: using the sealed $IMG"
  boot_start=$(part START 1); boot_sectors=$(part SECTORS 1)
  root_start=$(part START 2); root_sectors=$(part SECTORS 2)
else
  build_rootfs
fi

log "shrink"
attach
e2fsck -fy "$L2" >/dev/null || [ $? -le 1 ]
resize2fs -M "$L2" >/dev/null 2>&1
e2fsck -fy "$L2" >/dev/null || [ $? -le 1 ]
zerofree "$L2"
block_count=$(dumpe2fs -h "$L2" 2>/dev/null | awk -F': *' '/^Block count/ { print $2 }')
block_size=$(dumpe2fs -h "$L2" 2>/dev/null | awk -F': *' '/^Block size/ { print $2 }')
detach
new_sectors=$(( (block_count * block_size + 511) / 512 + SPARE_MB * 2048 ))
echo "${root_start},${new_sectors}" | sfdisk -N 2 --no-reread --no-tell-kernel -q "$IMG"
truncate -s $(( (root_start + new_sectors) * 512 )) "$IMG"
# The file system fills the (smaller) partition again; the Pi grows both to the card
root_sectors=$new_sectors
attach
resize2fs "$L2" >/dev/null 2>&1
e2fsck -fy "$L2" >/dev/null || [ $? -le 1 ]
detach
trap - EXIT

log "compress"
mkdir -p "$OUT"
xz -T0 -6 -c "$IMG" >"$OUT/$NAME.img.xz.part"
mv "$OUT/$NAME.img.xz.part" "$OUT/$NAME.img.xz"
(cd "$OUT" && sha256sum "$NAME.img.xz" >"$NAME.img.xz.sha256")
rm -f "$IMG"
ls -la "$OUT/$NAME.img.xz"
cat "$OUT/$NAME.img.xz.sha256"
log "done: $OUT/$NAME.img.xz"
