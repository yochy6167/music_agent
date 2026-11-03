"""Wi-Fi from the dashboard (NetworkManager / nmcli), without losing the player.

On a player that is only on Wi-Fi, a new network that does not reach the server
within 45 seconds is dropped and the previous connection is brought back.
Ethernet always wins (Wi-Fi connections get autoconnect-priority 10, as in
wifi_connect.sh). The password is never logged.
"""

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from oscmd import Result, run, run_sudo

logger = logging.getLogger(__name__)

Progress = Callable[[str, str], Awaitable[None]]
INTERNET_WAIT_S = 45
WIFI_PRIORITY = "10"


class NetError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def split_terse(line: str) -> List[str]:
    """nmcli -t fields: ':' separates, '\\:' is a colon inside a value."""
    return [part.replace("\\:", ":").replace("\\\\", "\\") for part in re.split(r"(?<!\\):", line)]


async def nmcli(args: List[str], timeout: float = 20.0) -> Result:
    """As the user first (polkit allows netdev members); with sudo -n if refused."""
    result = await run(["nmcli", *args], timeout=timeout)
    if not result.ok and re.search(r"not authorized|insufficient privileges|permission denied", result.text, re.I):
        result = await run_sudo(["nmcli", *args], timeout=timeout)
    return result


def band(freq: str) -> Optional[str]:
    match = re.search(r"\d+", freq or "")
    if not match:
        return None
    mhz = int(match.group())
    return "5GHz" if mhz >= 4900 else "2.4GHz"


async def wifi_iface() -> Optional[str]:
    result = await run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"], timeout=8)
    for line in result.out.splitlines():
        fields = split_terse(line)
        if len(fields) >= 2 and fields[1] == "wifi":
            return fields[0]
    return None


async def active_connections() -> List[Dict[str, str]]:
    result = await run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout=8)
    out = []
    for line in result.out.splitlines():
        fields = split_terse(line)
        if len(fields) >= 3 and fields[2] and fields[2] != "lo":
            out.append({"name": fields[0], "type": fields[1], "device": fields[2]})
    return out


async def scan() -> List[Dict[str, Any]]:
    iface = await wifi_iface()
    if not iface:
        raise NetError("no_wifi", "אין כרטיס Wi-Fi במכשיר.")
    result = await nmcli(
        ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", "ifname", iface, "--rescan", "yes"],
        timeout=30,
    )
    if not result.ok:
        raise NetError("wifi_scan_failed", "סריקת הרשתות נכשלה.", result.text[-500:])
    best: Dict[str, Dict[str, Any]] = {}
    for line in result.out.splitlines():
        fields = split_terse(line)
        if len(fields) < 5 or not fields[1]:
            continue
        in_use, ssid, signal, security, freq = fields[:5]
        entry = {
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else None,
            "security": security or None,
            "band": band(freq),
            "in_use": in_use.strip() == "*",
        }
        known = best.get(ssid)
        if known is None or (entry["signal"] or 0) > (known["signal"] or 0) or entry["in_use"]:
            entry["in_use"] = entry["in_use"] or bool(known and known["in_use"])
            best[ssid] = entry
    return sorted(best.values(), key=lambda e: (not e["in_use"], -(e["signal"] or 0)))


async def server_reachable(api_url: str, seconds: int) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{api_url.rstrip('/')}/health")
                if response.status_code < 500:
                    return True
        except httpx.HTTPError:
            pass
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(3)


async def connect(api_url: str, ssid: str, password: Optional[str], hidden: bool, progress: Progress) -> Dict[str, Any]:
    iface = await wifi_iface()
    if not iface:
        raise NetError("no_wifi", "אין כרטיס Wi-Fi במכשיר.")
    active = await active_connections()
    on_ethernet = any(c["type"] == "802-3-ethernet" for c in active)
    previous = next((c["name"] for c in active if c["type"] == "802-11-wireless"), None)
    if previous == ssid:
        previous = None

    await progress("connecting", f"מתחבר ל-{ssid}" + ("" if on_ethernet else " (המכשיר יתנתק לדקה)"))
    await nmcli(["connection", "delete", "id", ssid], timeout=10)  # a stale profile with an old password
    args = ["--wait", "40", "device", "wifi", "connect", ssid, "ifname", iface, "name", ssid]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    result = await nmcli(args, timeout=50)
    if result.ok:
        await nmcli(["connection", "modify", "id", ssid, "connection.autoconnect", "yes",
                     "connection.autoconnect-priority", WIFI_PRIORITY], timeout=10)
        await progress("checking", "בודק חיבור לשרת")
        if await server_reachable(api_url, INTERNET_WAIT_S):
            ip = await run(["nmcli", "-g", "IP4.ADDRESS", "device", "show", iface], timeout=8)
            return {"connected": True, "ssid": ssid, "ip": (ip.out.splitlines() or [""])[0].split("/")[0] or None}
        error = NetError("wifi_no_internet", f"מחובר ל-{ssid} אבל אין משם גישה לשרת.")
    else:
        text = result.text
        if re.search(r"secrets were required|802-11-wireless-security|wrong password|psk", text, re.I):
            error = NetError("wifi_auth_failed", "סיסמה שגויה.", text[-300:])
        elif re.search(r"no network with ssid|not found", text, re.I):
            error = NetError("wifi_not_found", f"הרשת {ssid} לא נמצאה.", text[-300:])
        else:
            error = NetError("wifi_connect_failed", "ההתחברות נכשלה.", text[-300:])

    # Back to where we were, unless the cable keeps the player online anyway
    await nmcli(["connection", "delete", "id", ssid], timeout=10)
    if previous and not on_ethernet:
        await progress("rollback", f"חוזר לרשת הקודמת {previous}")
        await nmcli(["--wait", "40", "connection", "up", "id", previous], timeout=50)
    raise error


async def forget(ssid: str) -> Dict[str, Any]:
    active = await active_connections()
    if any(c["name"] == ssid for c in active) and len(active) <= 1:
        raise NetError("only_connection", "זה החיבור היחיד של המכשיר; מחיקתו תנתק אותו.")
    result = await nmcli(["connection", "delete", "id", ssid], timeout=15)
    if not result.ok:
        raise NetError("wifi_forget_failed", "מחיקת הרשת נכשלה.", result.text[-300:])
    return {"removed": ssid}
