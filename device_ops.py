"""Device actions sent from the dashboard: a closed list, checked again here.

The server sends {"type": "agent_control", "action", "command_id", "params",
"expires_at"} over the WebSocket. Each command is answered with command_result
messages: accepted, then progress..., then ok or error. A command past its
expires_at is refused (a reboot clicked while the player was off must not run
when it returns), and a command_id already seen is ignored (the same command
can arrive over the socket and in pending_commands).

Nothing here starts music. After restart_player / reboot the agent asks the
server's playback-authorization, as on every start.
"""

import asyncio
import logging
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import health
import net_ops
from audio_output import BtError
from oscmd import STATE_DIR, needs_password, read_json, run, run_sudo, write_json_atomic

logger = logging.getLogger(__name__)

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
LOG_UNITS = ("music_agent", "bluetooth", "wireplumber", "pipewire", "NetworkManager")
PENDING_RESULTS = STATE_DIR / "pending_results.json"
PENDING_UPDATE = STATE_DIR / "pending_update.json"
LED_DIRS = (Path("/sys/class/leds/ACT"), Path("/sys/class/leds/led0"))

# The longest each action may run on the Pi
TIMEOUTS = {
    "bt_scan": 80, "bt_pair_connect": 140, "wifi_connect": 170, "wifi_scan": 45,
    "collect_logs": 60, "diagnose": 60,
}
DEFAULT_TIMEOUT = 45

ACTIONS = (
    "bt_scan", "bt_pair_connect", "bt_forget", "set_output", "set_volume", "test_tone",
    "restart_player", "reboot", "wifi_scan", "wifi_connect", "wifi_forget",
    "collect_logs", "diagnose", "identify",
)


class OpError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------- validation


def _int(params: dict, key: str, default: int, low: int, high: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise OpError("invalid_params", f"{key} חייב להיות בין {low} ל-{high}")
    return value


def _mac(value: Any) -> str:
    mac = str(value or "").strip().upper().replace("-", ":")
    if not MAC_RE.fullmatch(mac):
        raise OpError("invalid_params", "כתובת MAC לא תקינה")
    return mac


def _ssid(value: Any) -> str:
    ssid = str(value or "")
    if not ssid or len(ssid.encode("utf-8")) > 32:
        raise OpError("invalid_params", "שם רשת (SSID) באורך 1–32 בתים")
    return ssid


def validate(action: str, params: Optional[dict]) -> Dict[str, Any]:
    """Same rules as the server's device_ops.ACTIONS; anything unknown is refused."""
    params = params if isinstance(params, dict) else {}
    if action == "bt_scan":
        return {"seconds": _int(params, "seconds", 15, 5, 30)}
    if action == "bt_pair_connect":
        return {"mac": _mac(params.get("mac"))}
    if action == "bt_forget":
        return {"mac": _mac(params["mac"])} if params.get("mac") else {}
    if action == "set_output":
        mode = params.get("mode")
        if mode not in ("bluetooth", "aux", "hdmi", "auto"):
            raise OpError("invalid_params", "יציאה לא מוכרת")
        return {"mode": mode}
    if action == "set_volume":
        return {"volume": _int(params, "volume", -1, 0, 100)}
    if action == "wifi_connect":
        password = params.get("password") or None
        if password is not None and not 8 <= len(str(password)) <= 63:
            raise OpError("invalid_params", "סיסמת Wi-Fi באורך 8–63 תווים")
        return {"ssid": _ssid(params.get("ssid")), "password": password, "hidden": bool(params.get("hidden"))}
    if action == "wifi_forget":
        return {"ssid": _ssid(params.get("ssid"))}
    if action == "collect_logs":
        units = params.get("units") or ["music_agent"]
        if not isinstance(units, list) or any(u not in LOG_UNITS for u in units):
            raise OpError("invalid_params", "יחידת לוג לא מוכרת")
        return {"lines": _int(params, "lines", 200, 10, 1000), "units": list(dict.fromkeys(units))}
    if action == "identify":
        return {"seconds": _int(params, "seconds", 30, 5, 120)}
    if action in ACTIONS:
        return {}
    raise OpError("unknown_action", f"פעולה לא מוכרת: {action}")


def _expired(command: dict) -> bool:
    stamp = command.get("expires_at")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > when


# ---------------------------------------------------------------- identify (also while pending)


async def _write_led(path: Path, value: str) -> None:
    result = await run_sudo(["tee", str(path)], timeout=5, input_text=value)
    if not result.ok:
        if needs_password(result):
            raise OpError("sudo_required", "אין הרשאת sudo בלי סיסמה למכשיר הזה.")
        raise OpError("led_unavailable", "לא הצלחתי להפעיל את הנורה.", result.text[-200:])


async def identify_led(seconds: int) -> Dict[str, Any]:
    """Blink the green ACT LED, then give it back to the SD card activity trigger."""
    led = next((d for d in LED_DIRS if (d / "trigger").exists()), None)
    if led is None:
        raise OpError("led_unavailable", "אין למכשיר הזה נורת ACT (לא Raspberry Pi?).")
    text = (led / "trigger").read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"\[([^\]]+)\]", text)
    previous = match.group(1) if match else "mmc0"
    if previous == "timer":
        previous = "mmc0"
    await _write_led(led / "trigger", "timer")
    for name in ("delay_on", "delay_off"):
        if (led / name).exists():
            await _write_led(led / name, "150")

    global _led_restore
    _led_restore = (led / "trigger", previous)

    async def restore() -> None:
        await asyncio.sleep(seconds)
        await restore_led()

    asyncio.create_task(restore())
    return {"seconds": seconds, "led": led.name}


_led_restore: Optional[tuple] = None


async def restore_led() -> None:
    """Give the LED back now (also when the loop that would have done it is ending)."""
    global _led_restore
    if _led_restore is None:
        return
    path, previous = _led_restore
    _led_restore = None
    try:
        await _write_led(path, previous)
    except OpError as exc:
        logger.warning("Could not restore LED trigger: %s", exc.message)


# ---------------------------------------------------------------- dispatcher


class DeviceOps:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._seen: "deque[str]" = deque(maxlen=200)
        self._seen_set: set = set()

    def _remember(self, command_id: str) -> bool:
        """False when this command_id was handled already."""
        if command_id in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(command_id)
        self._seen_set.add(command_id)
        return True

    def claim(self, command: dict) -> bool:
        """Called on arrival: a repeated command_id is dropped before it is queued."""
        command_id = command.get("command_id")
        return not command_id or self._remember(str(command_id))

    # ------------------------------------------------------------ results

    async def send_result(self, command_id: Optional[str], status: str, **fields: Any) -> None:
        if not command_id:
            return
        message = {"type": "command_result", "command_id": command_id, "status": status,
                   **{k: v for k, v in fields.items() if v is not None}}
        ws = self.agent.ws_client
        if ws and ws.connected and await ws.send(message):
            return
        if await self.agent.client.post_command_result(message):
            return
        if status in ("ok", "error"):
            # Offline (a Wi-Fi switch): kept on disk and sent when the player is back
            pending = read_json(PENDING_RESULTS)
            pending = pending if isinstance(pending, list) else []
            write_json_atomic(PENDING_RESULTS, (pending + [message])[-50:], mode=0o600)

    async def flush_pending(self) -> None:
        """After (re)connecting: results kept on disk, and the end of an update."""
        pending = read_json(PENDING_RESULTS)
        if isinstance(pending, list) and pending:
            try:
                PENDING_RESULTS.unlink()
            except OSError:
                pass
            for message in pending:
                await self.send_result(message.get("command_id"), message.get("status"),
                                       **{k: v for k, v in message.items()
                                          if k not in ("type", "command_id", "status")})
        update = read_json(PENDING_UPDATE)
        if isinstance(update, dict) and update.get("command_id"):
            try:
                PENDING_UPDATE.unlink()
            except OSError:
                pass
            from agent import agent_version

            await self.send_result(update["command_id"], "ok", data={
                "version": agent_version(), "target": update.get("target"),
            }, message="העדכון הושלם")

    # ------------------------------------------------------------ handling

    async def handle(self, command: dict) -> None:
        action = str(command.get("action") or "")
        command_id = command.get("command_id")
        if _expired(command):
            logger.info("Ignoring expired %s command %s", action, command_id)
            await self.send_result(command_id, "error", error_code="expired", message="הפקודה פגה לפני שהגיעה")
            return
        try:
            params = validate(action, command.get("params"))
        except OpError as exc:
            await self.send_result(command_id, "error", error_code=exc.code, message=exc.message)
            return
        logger.info("Device action %s (%s)", action, command_id)
        await self.send_result(command_id, "accepted")

        async def progress(step: str, message: str) -> None:
            await self.send_result(command_id, "progress", step=step, message=message)

        try:
            data = await asyncio.wait_for(
                self._run(action, params, progress, command_id),
                timeout=TIMEOUTS.get(action, DEFAULT_TIMEOUT),
            )
        except asyncio.TimeoutError:
            await self.send_result(command_id, "error", error_code="timeout", message="הפעולה לא הסתיימה בזמן")
            return
        except (OpError, BtError, net_ops.NetError) as exc:
            logger.warning("Device action %s failed: %s %s", action, exc.code, exc.detail or exc.message)
            await self.send_result(command_id, "error", error_code=exc.code, message=exc.message,
                                   data={"detail": exc.detail} if exc.detail else None)
            return
        except Exception as exc:
            logger.exception("Device action %s crashed", action)
            await self.send_result(command_id, "error", error_code="internal", message=str(exc)[:300])
            return
        if data is not _DONE:
            await self.send_result(command_id, "ok", data=data)

    async def _run(self, action: str, params: dict, progress: Callable[[str, str], Awaitable[None]],
                   command_id: Optional[str]) -> Any:
        output = self.agent.output
        if action == "bt_scan":
            return {"devices": await output.scan(params["seconds"], progress)}
        if action == "bt_pair_connect":
            return await output.pair_connect(params["mac"], progress)
        if action == "bt_forget":
            return await output.forget(params.get("mac"))
        if action == "set_output":
            return await output.set_output(params["mode"])
        if action == "set_volume":
            return await output.set_volume(params["volume"])
        if action == "test_tone":
            return await output.test_tone()
        if action == "wifi_scan":
            return {"networks": await net_ops.scan()}
        if action == "wifi_connect":
            data = await net_ops.connect(self.agent.config.api_url, params["ssid"], params.get("password"),
                                         params["hidden"], progress)
            self.agent.request_health_push()
            return data
        if action == "wifi_forget":
            return await net_ops.forget(params["ssid"])
        if action == "collect_logs":
            return await health.collect_logs(params["units"], params["lines"])
        if action == "diagnose":
            checks = await health.selfcheck()
            self.agent.set_selfcheck(checks)
            return {"ok": all(c["ok"] for c in checks), "checks": checks}
        if action == "identify":
            return await identify_led(params["seconds"])
        if action == "restart_player":
            await self.send_result(command_id, "ok", message="הנגן מופעל מחדש")
            await self.agent.restart_process("restart requested from the dashboard")
            return _DONE
        if action == "reboot":
            probe = await run_sudo(["true"], timeout=5)
            if not probe.ok:
                raise OpError("sudo_required", "אין הרשאת sudo בלי סיסמה למכשיר הזה.", probe.text[-200:])
            await self.send_result(command_id, "ok", message="המכשיר עושה ריבוט")
            await self.agent.flush_ws(3.0)
            await asyncio.sleep(2)
            result = await run_sudo(["systemctl", "reboot"], timeout=15)
            if not result.ok:
                logger.error("Reboot failed: %s", result.text)
            return _DONE
        raise OpError("unknown_action", f"פעולה לא מוכרת: {action}")

    # ------------------------------------------------------------ update_software

    async def update_progress(self, command: dict, step: str, message: str) -> None:
        await self.send_result(command.get("command_id"), "progress", step=step, message=message)

    async def update_failed(self, command: dict, code: str, message: str) -> None:
        await self.send_result(command.get("command_id"), "error", error_code=code, message=message)

    def update_restarting(self, command: dict, target: str) -> None:
        """Before execv: the next start reports the update as done (with its version)."""
        if command.get("command_id"):
            write_json_atomic(PENDING_UPDATE, {
                "command_id": command["command_id"], "target": target or None, "started_at": int(time.time()),
            })


_DONE = object()  # the action sent its own final result
