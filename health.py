"""The player's health report (device_health) and its self-check (diagnose).

Every reading has a short timeout; one that fails is null, never an exception.
"""

import asyncio
import getpass
import logging
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import tailscale_ops
from oscmd import SYSTEM_DIR, read_json, run
from net_ops import band, split_terse

logger = logging.getLogger(__name__)

SELFCHECK_EVERY_S = 600


def image_version() -> Optional[str]:
    data = read_json(SYSTEM_DIR / "device.json")
    return data.get("image_version") if isinstance(data, dict) else None


def local_ip() -> Optional[str]:
    """The address of the interface with the default route (a UDP connect sends no packet)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _uptime() -> Optional[int]:
    text = _read("/proc/uptime")
    try:
        return int(float(text.split()[0])) if text else None
    except (ValueError, IndexError):
        return None


def _cpu_temp() -> Optional[float]:
    text = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(text) / 1000, 1) if text else None
    except ValueError:
        return None


def _mem_used_pct() -> Optional[int]:
    text = _read("/proc/meminfo") or ""
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        try:
            values[key] = int(rest.split()[0])
        except (IndexError, ValueError):
            continue
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return round(100 * (total - available) / total)


def _disk() -> Optional[Dict[str, int]]:
    try:
        usage = shutil.disk_usage("/")
    except OSError:
        return None
    return {"free_mb": usage.free // (1024 * 1024), "used_pct": round(100 * usage.used / usage.total)}


async def _throttled() -> Optional[str]:
    result = await run(["vcgencmd", "get_throttled"], timeout=3)
    if result.ok and "=" in result.out:
        return result.out.strip().split("=", 1)[1]
    return None


async def _clock_synced() -> Optional[bool]:
    result = await run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=3)
    return result.out.strip() == "yes" if result.ok else None


async def network() -> Dict[str, Any]:
    info: Dict[str, Any] = {"iface": None, "type": None, "ssid": None, "signal": None, "band": None,
                            "local_ip": local_ip()}
    result = await run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=3)
    connected = [split_terse(line) for line in result.out.splitlines()]
    connected = [f for f in connected if len(f) >= 3 and f[2] == "connected" and f[1] in ("ethernet", "wifi")]
    # Ethernet first: it is what the player uses when both are up
    connected.sort(key=lambda f: f[1] != "ethernet")
    if not connected:
        return info
    iface, kind = connected[0][0], connected[0][1]
    info.update({"iface": iface, "type": kind})
    if kind == "wifi":
        wifi = await run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,FREQ", "device", "wifi", "list", "ifname", iface,
                          "--rescan", "no"], timeout=3)
        for line in wifi.out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 4 and fields[0] == "yes":
                info.update({"ssid": fields[1], "signal": int(fields[2]) if fields[2].isdigit() else None,
                             "band": band(fields[3])})
                break
    return info


async def collect(agent_version: str, output: Any, selfcheck_ok: Optional[bool]) -> Dict[str, Any]:
    throttled, clock, net, tailscale = await asyncio.gather(
        _throttled(), _clock_synced(), network(), tailscale_ops.status()
    )
    report: Dict[str, Any] = {
        "agent_version": agent_version,
        "image_version": image_version(),
        "uptime_s": _uptime(),
        "cpu_temp_c": _cpu_temp(),
        "throttled": throttled,
        "disk": _disk(),
        "mem_used_pct": _mem_used_pct(),
        "clock_synced": clock,
        "network": net,
        "tailscale": tailscale,
        "selfcheck_ok": selfcheck_ok,
        "collected_at": int(time.time()),
    }
    if output is not None:
        report.update(output.snapshot())
    return report


# ---------------------------------------------------------------- self-check


def _check(check_id: str, ok: bool, hint_he: str, detail: str = "") -> Dict[str, Any]:
    return {"id": check_id, "ok": bool(ok), "hint_he": None if ok else hint_he, "detail": detail or None}


async def selfcheck() -> List[Dict[str, Any]]:
    user = getpass.getuser()
    checks: List[Dict[str, Any]] = []

    groups = await run(["id", "-nG"], timeout=3)
    checks.append(_check("user_in_bluetooth_group", "bluetooth" in groups.out.split(),
                         f"המשתמש {user} לא בקבוצת bluetooth: sudo usermod -aG bluetooth {user} ואז ריבוט."))

    bt_active = (await run(["systemctl", "is-active", "bluetooth"], timeout=3)).out.strip() == "active"
    checks.append(_check("bluetooth_service", bt_active, "שירות הבלוטוס לא רץ: sudo systemctl enable --now bluetooth."))
    if bt_active:
        show = await run(["bluetoothctl", "show"], timeout=5)
        checks.append(_check("bt_adapter_powered", "Powered: yes" in show.out,
                             "מתאם הבלוטוס כבוי או חסום (rfkill unblock bluetooth).", show.text[-200:]))

    for unit in ("pipewire", "pipewire-pulse", "wireplumber"):
        state = (await run(["systemctl", "--user", "is-active", unit], timeout=3)).out.strip()
        checks.append(_check(f"user_{unit}", state == "active",
                             f"{unit} לא רץ למשתמש: systemctl --global enable {unit} ו-linger.", state))

    spa = await run(["dpkg", "-s", "libspa-0.2-bluetooth"], timeout=5)
    checks.append(_check("libspa_bluetooth", spa.ok and "install ok installed" in spa.out,
                         "חסרה החבילה libspa-0.2-bluetooth (בלוטוס שמע ב-PipeWire)."))

    conf_dir = Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d"
    seat_fix = False
    for conf in conf_dir.glob("*.conf") if conf_dir.is_dir() else []:
        try:
            text = conf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "seat-monitoring" in text and "disabled" in text:
            seat_fix = True
    checks.append(_check("wireplumber_seat_fix", seat_fix,
                         "חסר 50-bluez.conf עם monitor.bluez.seat-monitoring = disabled (בלי מסך, בלוטוס שמע לא עולה)."))

    checks.append(_check("linger", Path(f"/var/lib/systemd/linger/{user}").exists(),
                         f"linger כבוי: sudo loginctl enable-linger {user}."))

    info = await run(["pactl", "info"], timeout=5)
    checks.append(_check("pactl_info", info.ok, "שרת השמע לא עונה ל-pactl.", info.text[-200:]))
    default_sink = await run(["pactl", "get-default-sink"], timeout=5)
    checks.append(_check("default_sink", default_sink.ok and bool(default_sink.out.strip()),
                         "אין פלט שמע ברירת מחדל."))

    checks.append(_check("journald_persistent", Path("/var/log/journal").is_dir(),
                         "היומן לא נשמר בין עליות: Storage=persistent ב-journald.conf."))
    return checks


async def collect_logs(units: List[str], lines: int) -> Dict[str, Any]:
    """journalctl for the chosen units; PipeWire / WirePlumber are user units."""
    user_units = {"wireplumber", "pipewire"}
    parts = []
    for unit in units:
        flag = "--user-unit" if unit in user_units else "-u"
        result = await run(["journalctl", flag, unit, "-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=15)
        body = result.out if result.out.strip() else (result.err or "(אין שורות)")
        parts.append(f"===== {unit} =====\n{body.rstrip()}")
    text = "\n\n".join(parts)
    encoded = text.encode("utf-8")
    limit = 256 * 1024
    truncated = len(encoded) > limit
    if truncated:
        text = encoded[-limit:].decode("utf-8", errors="ignore")
    return {"text": text, "units": units, "lines": lines, "truncated": truncated}
