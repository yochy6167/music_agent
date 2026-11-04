"""Tailscale on the player: its state for the health report, and joining the tailnet.

The SD image installs Tailscale logged out. Once the player is assigned to a
branch, the server sends tailscale_join with a one-time key it minted; the key
goes to tailscale through a 0600 file (not argv, which any user can read) and
the file is deleted at once. --ssh turns on Tailscale SSH, the support route.
A Pi installed with setup.sh has no Tailscale: its report says installed=false.
"""

import json
import os
import re
import tempfile
from typing import Any, Awaitable, Callable, Dict, Optional

from oscmd import run, run_sudo

AUTH_KEY_RE = re.compile(r"^tskey-[A-Za-z0-9_-]{10,200}$")
HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
JOIN_TIMEOUT_S = 90


class TailscaleError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


async def status() -> Dict[str, Any]:
    """{installed, state, ip, dns_name, online}. state is Tailscale's BackendState
    (NeedsLogin, Running, Stopped...) or "daemon_down"."""
    result = await run(["tailscale", "status", "--json", "--peers=false"], timeout=5)
    if result.code == 127:
        return {"installed": False}
    try:
        data = json.loads(result.out)
    except ValueError:
        return {"installed": True, "state": "daemon_down"}
    me = data.get("Self") or {}
    ips = data.get("TailscaleIPs") or me.get("TailscaleIPs") or []
    return {
        "installed": True,
        "state": data.get("BackendState"),
        "ip": next((ip for ip in ips if "." in ip), None),
        "dns_name": (me.get("DNSName") or "").rstrip(".") or None,
        "online": me.get("Online"),
    }


async def join(auth_key: str, hostname: str, progress: Callable[[str, str], Awaitable[None]]) -> Dict[str, Any]:
    before = await status()
    if not before.get("installed"):
        raise TailscaleError("tailscale_missing", "Tailscale לא מותקן על המכשיר.")
    await progress("joining", "מתחבר ל-Tailscale…")
    fd, path = tempfile.mkstemp(prefix="ts-key-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(auth_key)
        # --reset: the image's defaults plus these flags, whatever was set before.
        # --accept-dns=false: the shop's own DNS stays (MagicDNS is not needed for SSH).
        result = await run_sudo([
            "tailscale", "up", "--reset", f"--auth-key=file:{path}", "--ssh",
            f"--hostname={hostname}", "--accept-dns=false", f"--timeout={JOIN_TIMEOUT_S - 15}s",
        ], timeout=JOIN_TIMEOUT_S)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    after = await status()
    if after.get("state") != "Running":
        if "password is required" in result.err:
            raise TailscaleError("sudo_required", "אין הרשאת sudo בלי סיסמה למכשיר הזה.")
        raise TailscaleError("tailscale_failed", "החיבור ל-Tailscale נכשל.", result.text[-400:])
    return after


def valid_key(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and AUTH_KEY_RE.fullmatch(value) else None


def valid_hostname(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and HOSTNAME_RE.fullmatch(value) else None
