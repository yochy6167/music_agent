#!/bin/bash
# Runs INSIDE the image (chroot, arm64 under qemu), called by build-image.sh.
# Installs everything a player needs, so a shop network never needs apt
# (archive.raspberrypi.com is blocked in some). Nothing here may make an
# identity: hostname, SSH host keys, claim_secret and machine-id are made on
# each Pi by soundops-firstboot.service, Tailscale stays logged out.
set -euo pipefail

: "${AGENT_REF:?}" "${AGENT_REPO:?}" "${IMAGE_VERSION:?}"
export DEBIAN_FRONTEND=noninteractive
log() { echo "==> [rootfs] $*"; }
as_pi() { runuser -u pi -- env HOME=/home/pi "$@"; }

log "apt packages"
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip \
  vlc libvlc-dev \
  git curl ca-certificates jq unzip rfkill \
  alsa-utils libasound2-plugins \
  pipewire pipewire-pulse pipewire-alsa wireplumber libspa-0.2-bluetooth pulseaudio-utils \
  bluez network-manager openssh-server avahi-daemon

log "Tailscale (installed, logged out)"
. /etc/os-release
codename="${VERSION_CODENAME:-trixie}"
if ! curl -fsSL "https://pkgs.tailscale.com/stable/debian/${codename}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg; then
  codename=bookworm
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/${codename}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
fi
curl -fsSL "https://pkgs.tailscale.com/stable/debian/${codename}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
apt-get update -qq
apt-get install -y -qq tailscale
systemctl enable tailscaled.service

log "user pi: shell, groups, locked password"
usermod -s /bin/bash pi
passwd -l pi >/dev/null
for group in sudo audio video bluetooth netdev plugdev input render gpio; do
  getent group "$group" >/dev/null && usermod -aG "$group" pi
done
# No first-boot wizard asking for a user on a screen nobody has
systemctl disable userconfig.service 2>/dev/null || true
rm -f /etc/ssh/sshd_config.d/rename_user.conf
chmod 0440 /etc/sudoers.d/010_pi-nopasswd
visudo -cf /etc/sudoers.d/010_pi-nopasswd >/dev/null

log "SSH on (key only), cloud-init off"
systemctl enable ssh.service
systemctl disable sshswitch.service 2>/dev/null || true
touch /etc/cloud/cloud-init.disabled

log "time zone Asia/Jerusalem"
ln -sf /usr/share/zoneinfo/Asia/Jerusalem /etc/localtime
echo "Asia/Jerusalem" >/etc/timezone

log "agent ${AGENT_REF} from ${AGENT_REPO}"
rm -rf /home/pi/music_agent
as_pi git clone --quiet "$AGENT_REPO" /home/pi/music_agent
cd /home/pi/music_agent
as_pi git -c advice.detachedHead=false checkout --quiet --detach "$AGENT_REF"
as_pi git config core.fileMode false
as_pi python3 -m venv .venv
as_pi .venv/bin/pip install --quiet --upgrade pip
as_pi .venv/bin/pip install --quiet -r requirements.txt
as_pi .venv/bin/pip install --quiet -U "yt-dlp[default]"
# api_url / ws_url only: no device yet (the Pi registers and waits for approval)
as_pi python3 - <<'EOF'
import json
data = json.load(open("config.json.example", encoding="utf-8"))
data["devices"] = []
json.dump(data, open("config.json", "w", encoding="utf-8"), indent=2)
EOF

log "Deno (yt-dlp YouTube challenges)"
curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-aarch64-unknown-linux-gnu.zip
install -d -o pi -g pi /home/pi/.deno /home/pi/.deno/bin
unzip -oq /tmp/deno.zip -d /home/pi/.deno/bin
chown pi:pi /home/pi/.deno/bin/deno
chmod 0755 /home/pi/.deno/bin/deno
rm -f /tmp/deno.zip

log "audio session for a headless Pi: linger + PipeWire user units"
install -d /var/lib/systemd/linger
touch /var/lib/systemd/linger/pi
systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service
chown -R pi:pi /home/pi

log "services"
install -d -m 2755 -g systemd-journal /var/log/journal
systemctl enable \
  soundops-firstboot.service soundops-wifi.service \
  music_agent.service music-agent-nightly-reboot.timer music-agent-ytdlp-update.timer \
  bluetooth.service NetworkManager.service avahi-daemon.service
# The legacy Bluetooth keeper of setup.sh installs: the agent does its job
systemctl disable bt-keep.service 2>/dev/null || true

log "boot config: HDMI audio without a monitor, Wi-Fi country IL"
grep -qE '^hdmi_force_hotplug=1' /boot/firmware/config.txt || echo "hdmi_force_hotplug=1" >>/boot/firmware/config.txt
grep -q 'cfg80211.ieee80211_regdom=' /boot/firmware/cmdline.txt || \
  sed -i '1 s/$/ cfg80211.ieee80211_regdom=IL/' /boot/firmware/cmdline.txt

log "self-check"
fail=0
check() { if eval "$2" >/dev/null 2>&1; then echo "  PASS $1"; else echo "  FAIL $1"; fail=1; fi; }
check "python deps"           ".venv/bin/python3 -c 'import vlc, httpx, websockets, yt_dlp'"
check "agent at ${AGENT_REF}" "[ \"\$(as_pi git rev-parse HEAD)\" = \"\$(as_pi git rev-parse '${AGENT_REF}^{commit}')\" ]"
check "config without device" "jq -e '.devices == [] and (.api_url | length > 0)' config.json"
check "deno binary"           "[ -x /home/pi/.deno/bin/deno ]"
check "tailscale"             "command -v tailscale && systemctl is-enabled tailscaled"
check "pactl / pacat"         "command -v pactl && command -v pacat"
check "ALSA default->PipeWire" "ls /usr/share/alsa/alsa.conf.d/99-pipewire-default.conf || ls /etc/alsa/conf.d/99-pipewire-default.conf"
check "libspa bluetooth"      "dpkg -s libspa-0.2-bluetooth"
check "wireplumber seat fix"  "grep -q seat-monitoring /home/pi/.config/wireplumber/wireplumber.conf.d/50-bluez.conf"
check "linger"                "[ -e /var/lib/systemd/linger/pi ]"
check "music_agent enabled"   "systemctl is-enabled music_agent.service"
check "firstboot enabled"     "systemctl is-enabled soundops-firstboot.service"
check "userconfig disabled"   "! systemctl is-enabled userconfig.service"
check "ssh enabled"           "systemctl is-enabled ssh.service"
check "pi in groups"          "id -nG pi | grep -qw bluetooth && id -nG pi | grep -qw audio && id -nG pi | grep -qw netdev"
check "pi password locked"    "passwd -S pi | awk '{exit !(\$2==\"L\")}'"
echo "  python $(python3 -V 2>&1 | cut -d' ' -f2), vlc $(dpkg-query -W -f='${Version}' vlc), tailscale $(dpkg-query -W -f='${Version}' tailscale), agent $(as_pi git describe --tags --always)"
[ "$fail" = 0 ] || { echo "==> [rootfs] self-check FAILED"; exit 1; }
log "done"
