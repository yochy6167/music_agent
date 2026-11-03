"""Bluetooth speaker and audio output, kept by the agent (replaces bt-keep.service,
bt-pair.sh and the sink choice in scripts/set-system-volume.sh).

State lives in ~/.config/soundops/audio.json: {"mode", "bt_mac", "bt_name", "sink_volume"}.
Without a mode the player is not managed here (an older install keeps whatever
set-system-volume.sh chose) until the dashboard sets an output or pairs a speaker.

Every 15 seconds: reconnect the saved speaker if it dropped, and make the wanted
sink the default, moving the streams that play to it. Sinks are picked by name,
never by the numbers in `wpctl status` (they change on every boot, and the
Devices and Sinks lists share them: that is what broke on 2 Nov).
WirePlumber's own choice is not relied on: it locks onto whatever came up first.
"""

import asyncio
import json
import logging
import math
import re
import struct
import time
import wave
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import oscmd
from oscmd import STATE_DIR, read_json, run, run_sudo, write_json_atomic

logger = logging.getLogger(__name__)

AUDIO_STATE = STATE_DIR / "audio.json"
LEGACY_SPEAKER_CONF = Path("/etc/bt-speaker.conf")
LEGACY_SERVICE = "bt-keep.service"
MODES = ("bluetooth", "aux", "hdmi", "auto")
MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")

KEEPALIVE_S = 15
KEEPALIVE_SLOW_S = 60  # after many failed reconnects: less radio noise next to 2.4GHz Wi-Fi
SLOW_AFTER_FAILURES = 20
DEFAULT_SINK_VOLUME = 90

Progress = Callable[[str, str], Awaitable[None]]


class BtError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# bluetoothctl / BlueZ text -> error_code (the dashboard explains each one in Hebrew)
_BT_ERRORS = [
    ("br-connection-profile-unavailable", "bt_no_audio_profile",
     "אין שירות שמע שמקבל בלוטוס (PipeWire / WirePlumber / libspa-0.2-bluetooth). להריץ אבחון."),
    ("br-connection-page-timeout", "bt_speaker_unreachable",
     "הרמקול לא עונה: כבוי, רחוק, מחובר לטלפון אחר או לא במצב בלוטוס."),
    ("not-powered", "bt_adapter_off", "מתאם הבלוטוס כבוי."),
    ("NotReady", "bt_adapter_off", "מתאם הבלוטוס עוד לא מוכן."),
    ("AuthenticationFailed", "bt_pairing_rejected", "הרמקול דחה את הצימוד: להכניס אותו למצב צימוד."),
    ("AuthenticationCanceled", "bt_pairing_rejected", "הרמקול דחה את הצימוד: להכניס אותו למצב צימוד."),
    ("AuthenticationRejected", "bt_pairing_rejected", "הרמקול דחה את הצימוד: להכניס אותו למצב צימוד."),
    ("not available", "bt_not_found", "הרמקול לא נמצא בסריקה: להכניס למצב צימוד ולסרוק שוב."),
    ("InProgress", "bt_busy", "הבלוטוס עסוק בפעולה אחרת."),
    ("br-connection-busy", "bt_busy", "הבלוטוס עסוק בפעולה אחרת."),
]


def bt_error(text: str) -> BtError:
    for needle, code, message in _BT_ERRORS:
        if needle.lower() in text.lower():
            return BtError(code, message, text[-500:])
    return BtError("bt_unknown", "שגיאת בלוטוס", text[-500:])


def sink_prefix(mac: str) -> str:
    return "bluez_output." + mac.upper().replace(":", "_")


def sink_kind(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    low = name.lower()
    if low.startswith("bluez_output."):
        return "bluetooth"
    if "hdmi" in low:
        return "hdmi"
    if "bcm2835" in low or "headphones" in low or "analog" in low:
        return "aux"
    return "other"


class OutputManager:
    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self.on_change = on_change
        self.lock = asyncio.Lock()  # the keep-alive waits while a scan / pairing runs
        self.state: Dict[str, Any] = {}
        self.connected = False
        self.connected_since: Optional[float] = None
        self.adapter_powered: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.default_sink: Optional[str] = None
        self.sink_volume: Optional[int] = None
        self.sink_muted: Optional[bool] = None
        self._volume_set_on: Optional[str] = None
        self._failures = 0
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ state

    @property
    def mode(self) -> Optional[str]:
        mode = self.state.get("mode")
        return mode if mode in MODES else None

    def _load(self) -> None:
        data = read_json(AUDIO_STATE)
        self.state = data if isinstance(data, dict) else {}

    def _save(self) -> None:
        write_json_atomic(AUDIO_STATE, self.state)

    async def _migrate_legacy(self) -> None:
        """Import the speaker of bt-keep.service once, and stop that service: two keepers fight."""
        if not AUDIO_STATE.exists() and LEGACY_SPEAKER_CONF.exists():
            try:
                match = MAC_RE.search(LEGACY_SPEAKER_CONF.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                match = None
            if match:
                self.state = {"mode": "bluetooth", "bt_mac": match.group(1).upper(), "sink_volume": DEFAULT_SINK_VOLUME}
                self._save()
                logger.info("Imported Bluetooth speaker %s from %s", self.state["bt_mac"], LEGACY_SPEAKER_CONF)
        if self.mode:
            await self._disable_legacy_service()

    async def _disable_legacy_service(self) -> None:
        """Once the agent manages the output, bt-keep.service must stop: two keepers fight."""
        probe = await run(["systemctl", "is-enabled", LEGACY_SERVICE], timeout=5)
        active = await run(["systemctl", "is-active", LEGACY_SERVICE], timeout=5)
        if probe.out.strip() in ("enabled", "enabled-runtime") or active.out.strip() == "active":
            result = await run_sudo(["systemctl", "disable", "--now", LEGACY_SERVICE], timeout=20)
            if result.ok:
                logger.info("Disabled %s: the agent keeps the speaker now", LEGACY_SERVICE)
            else:
                logger.warning("Could not disable %s: %s", LEGACY_SERVICE, result.text)

    async def start(self) -> None:
        self._load()
        try:
            await self._migrate_legacy()
        except Exception as exc:
            logger.warning("Audio state migration failed: %s", exc)
        self._task = asyncio.create_task(self._loop())

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception as exc:
                logger.debug("on_change failed: %s", exc)

    # ------------------------------------------------------------ keep-alive

    async def _loop(self) -> None:
        while True:
            try:
                async with self.lock:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Audio keep-alive failed: %s", exc)
            await asyncio.sleep(KEEPALIVE_SLOW_S if self._failures >= SLOW_AFTER_FAILURES else KEEPALIVE_S)

    async def tick(self) -> None:
        """One keep-alive round. Caller holds the lock."""
        before = (self.connected, self.default_sink, self.last_error)
        mac = self.state.get("bt_mac")
        if self.mode in ("bluetooth", "auto") and mac:
            await self._keep_speaker(mac)
        else:
            self.connected, self.connected_since = False, None
        if self.mode:
            await self.apply_output()
        else:
            self.default_sink = await self.get_default_sink()
            self._read_sink(await self.list_sinks())
        if before != (self.connected, self.default_sink, self.last_error):
            self._changed()

    async def _keep_speaker(self, mac: str) -> None:
        if not await self.bt_service_ok():
            self.connected, self.connected_since = False, None
            self.last_error = "bt_service_down"
            self._failures += 1
            return
        info = await self.info(mac)
        if info.get("connected"):
            if not self.connected:
                self.connected_since = time.time()
                logger.info("Speaker %s connected", mac)
            self.connected, self.last_error, self._failures = True, None, 0
            return
        if self.connected:
            logger.warning("Speaker %s disconnected", mac)
        self.connected, self.connected_since = False, None
        result = await self.btctl(["connect", mac], timeout=12)
        if result.ok and "Connection successful" in result.out:
            self.connected, self.connected_since = True, time.time()
            self.last_error, self._failures = None, 0
            logger.info("Speaker %s reconnected", mac)
        else:
            self.last_error = bt_error(result.text).code if not result.timed_out else "bt_speaker_unreachable"
            self._failures += 1

    # ------------------------------------------------------------ sinks

    async def list_sinks(self) -> List[Dict[str, Any]]:
        """[{name, available, volume}]; JSON where pactl has it, else the short list."""
        result = await run(["pactl", "-f", "json", "list", "sinks"], timeout=5)
        if result.ok:
            try:
                sinks = []
                for sink in json.loads(result.out):
                    ports = sink.get("ports") or []
                    available = not ports or any(
                        (p.get("availability") or "") != "not available" for p in ports
                    )
                    volume = None
                    for channel in (sink.get("volume") or {}).values():
                        try:
                            volume = int(str(channel.get("value_percent", "")).rstrip("%"))
                        except (ValueError, AttributeError):
                            pass
                        break
                    sinks.append({"name": sink.get("name"), "available": available, "volume": volume,
                                  "mute": bool(sink.get("mute"))})
                return sinks
            except (ValueError, TypeError):
                pass
        result = await run(["pactl", "list", "short", "sinks"], timeout=5)
        sinks = []
        for line in result.out.splitlines():
            parts = line.split("\t")
            if len(parts) > 1:
                sinks.append({"name": parts[1], "available": True, "volume": None, "mute": None})
        return sinks

    async def get_default_sink(self) -> Optional[str]:
        result = await run(["pactl", "get-default-sink"], timeout=5)
        return result.out.strip() or None if result.ok else None

    def choose_sink(self, sinks: List[Dict[str, Any]]) -> Optional[str]:
        names = [s["name"] for s in sinks if s.get("name")]
        mac = self.state.get("bt_mac")
        bt = next((n for n in names if mac and n.upper().startswith(sink_prefix(mac).upper())), None)
        aux = next((n for n in names if sink_kind(n) == "aux"), None)
        hdmi_live = next((s["name"] for s in sinks if sink_kind(s.get("name")) == "hdmi" and s.get("available")), None)
        hdmi = hdmi_live or next((n for n in names if sink_kind(n) == "hdmi"), None)
        first = next((n for n in names if sink_kind(n) != "bluetooth"), names[0] if names else None)
        mode = self.mode
        if mode == "bluetooth":
            return bt or aux or first
        if mode == "aux":
            return aux or first
        if mode == "hdmi":
            return hdmi or aux or first
        # auto: the speaker, else a connected HDMI, else the jack
        return bt or hdmi_live or aux or first

    async def apply_output(self) -> Optional[str]:
        """Make the wanted sink the default and move what plays onto it. Caller holds the lock."""
        sinks = await self.list_sinks()
        wanted = self.choose_sink(sinks)
        current = await self.get_default_sink()
        if wanted and wanted != current:
            result = await run(["pactl", "set-default-sink", wanted], timeout=5)
            if result.ok:
                logger.info("Audio output: %s (was %s, mode %s)", wanted, current, self.mode)
                current = wanted
                inputs = await run(["pactl", "list", "short", "sink-inputs"], timeout=5)
                for line in inputs.out.splitlines():
                    stream = line.split("\t")[0].strip()
                    if stream.isdigit():
                        await run(["pactl", "move-sink-input", stream, wanted], timeout=5)
            else:
                logger.warning("Could not switch output to %s: %s", wanted, result.text)
        if current and current != self._volume_set_on:
            # A sink that just appeared comes up at its own level (the speaker rose at 40%, not 90%)
            volume = int(self.state.get("sink_volume") or DEFAULT_SINK_VOLUME)
            if (await run(["pactl", "set-sink-volume", current, f"{volume}%"], timeout=5)).ok:
                self._volume_set_on = current
                # The list above was read before this change
                for sink in sinks:
                    if sink.get("name") == current:
                        sink["volume"] = volume
        self.default_sink = current
        self._read_sink(sinks)
        return current

    def _read_sink(self, sinks: List[Dict[str, Any]]) -> None:
        for sink in sinks:
            if sink.get("name") == self.default_sink:
                self.sink_volume, self.sink_muted = sink.get("volume"), sink.get("mute")

    # ------------------------------------------------------------ bluetooth

    async def bt_service_ok(self) -> bool:
        return (await run(["systemctl", "is-active", "bluetooth"], timeout=5)).out.strip() == "active"

    async def btctl(self, args: List[str], timeout: float = 10.0) -> oscmd.Result:
        return await run(["bluetoothctl", *args], timeout=timeout)

    async def _require_bt(self) -> None:
        if not await self.bt_service_ok():
            raise BtError("bt_service_down", "שירות הבלוטוס לא פעיל במכשיר.")

    async def _power_on(self) -> None:
        await run(["rfkill", "unblock", "bluetooth"], timeout=5)
        show = await self.btctl(["show"], timeout=6)
        if "Powered: yes" not in show.out:
            await self.btctl(["power", "on"], timeout=8)
            show = await self.btctl(["show"], timeout=6)
        self.adapter_powered = "Powered: yes" in show.out
        if not self.adapter_powered:
            raise BtError("bt_adapter_off", "מתאם הבלוטוס כבוי ולא נדלק.", show.text)

    async def info(self, mac: str) -> Dict[str, Any]:
        result = await self.btctl(["info", mac], timeout=6)
        text = result.out
        info: Dict[str, Any] = {"mac": mac.upper()}
        if not result.ok or "not available" in text:
            return info
        for line in text.splitlines():
            line = line.strip()
            key, _, value = line.partition(":")
            value = value.strip()
            if key == "Name":
                info["name"] = value
            elif key in ("Paired", "Trusted", "Connected"):
                info[key.lower()] = value == "yes"
            elif key == "RSSI":
                match = re.search(r"-?\d+", value)
                info["rssi"] = int(match.group()) if match else None
            elif key == "Icon":
                info["icon"] = value
            elif key == "UUID" and "0000110b" in value.lower():
                info["a2dp"] = True
        info["is_audio"] = bool(info.get("a2dp") or str(info.get("icon", "")).startswith("audio"))
        return info

    async def scan(self, seconds: int, progress: Progress) -> List[Dict[str, Any]]:
        async with self.lock:
            await self._require_bt()
            await self._power_on()
            await progress("scanning", f"מחפש רמקולים {seconds} שניות. שים את הרמקול במצב צימוד.")
            await self.btctl(["--timeout", str(seconds), "scan", "on"], timeout=seconds + 8)
            listing = await self.btctl(["devices"], timeout=6)
            macs = []
            for line in listing.out.splitlines():
                match = re.match(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2})\s*(.*)", line.strip(), re.I)
                if match:
                    macs.append(match.group(1).upper())
            await progress("reading", f"נמצאו {len(macs)} מכשירים, קורא פרטים")
            gate = asyncio.Semaphore(4)

            async def one(mac: str) -> Dict[str, Any]:
                async with gate:
                    return await self.info(mac)

            found = await asyncio.gather(*(one(m) for m in macs[:40]))
        out = []
        for d in found:
            name = d.get("name") or ""
            # Nameless devices show their MAC as the name; nobody can pick those
            if not name or name.replace("-", ":").upper() == d["mac"]:
                continue
            out.append({
                "mac": d["mac"], "name": name, "rssi": d.get("rssi"),
                "paired": bool(d.get("paired")), "trusted": bool(d.get("trusted")),
                "connected": bool(d.get("connected")), "is_audio": d["is_audio"],
            })
        out.sort(key=lambda d: (not d["is_audio"], -(d["rssi"] if d["rssi"] is not None else -999)))
        return out

    async def _bt_step(self, args: List[str], timeout: float, success: str, tolerate: tuple = ()) -> str:
        """One bluetoothctl step, retried once after 5s when BlueZ is busy."""
        for attempt in (1, 2):
            result = await self.btctl(args, timeout=timeout)
            if success in result.out or any(t in result.text for t in tolerate):
                return result.out
            if result.timed_out:
                raise BtError("bt_speaker_unreachable", "הרמקול לא ענה בזמן.", result.text)
            error = bt_error(result.text)
            if error.code == "bt_busy" and attempt == 1:
                await asyncio.sleep(5)
                continue
            raise error
        raise BtError("bt_busy", "הבלוטוס עסוק בפעולה אחרת.")

    async def pair_connect(self, mac: str, progress: Progress) -> Dict[str, Any]:
        mac = mac.upper()
        async with self.lock:
            await self._require_bt()
            await self._power_on()
            old = self.state.get("bt_mac")
            if old and old != mac:
                await progress("removing_old", f"מסיר את הרמקול הקודם {self.state.get('bt_name') or old}")
                await self.btctl(["remove", old], timeout=8)
            await progress("removing", "מנקה צימוד ישן")
            await self.btctl(["remove", mac], timeout=8)  # "not available" is fine
            await progress("searching", "מחפש את הרמקול")
            await self.btctl(["--timeout", "10", "scan", "on"], timeout=18)
            info = await self.info(mac)
            if not info.get("name") and "rssi" not in info:
                raise BtError("bt_not_found", "הרמקול לא נמצא בסריקה: להכניס למצב צימוד ולסרוק שוב.")
            await progress("pairing", "מצמד")
            await self._bt_step(["pair", mac], 30, "Pairing successful", tolerate=("AlreadyExists",))
            await progress("trusting", "מסמן כמכשיר מהימן")
            await self._bt_step(["trust", mac], 8, "trust succeeded")
            await progress("connecting", "מתחבר")
            await self._bt_step(["connect", mac], 15, "Connection successful")
            await progress("routing", "מעביר את הסאונד לרמקול")
            sink = None
            for _ in range(20):
                names = [s["name"] for s in await self.list_sinks()]
                sink = next((n for n in names if n.upper().startswith(sink_prefix(mac).upper())), None)
                if sink:
                    break
                await asyncio.sleep(0.5)
            if not sink:
                raise BtError("bt_no_audio_profile",
                              "הרמקול מחובר אבל לא הופיע כפלט שמע (PipeWire / libspa-0.2-bluetooth). להריץ אבחון.")
            self.state.update({"mode": "bluetooth", "bt_mac": mac, "bt_name": info.get("name") or mac})
            self.state.setdefault("sink_volume", DEFAULT_SINK_VOLUME)
            self._save()
            self.connected, self.connected_since, self.last_error, self._failures = True, time.time(), None, 0
            self._volume_set_on = None
            current = await self.apply_output()
        await self._disable_legacy_service()
        self._changed()
        return {"connected": True, "name": self.state["bt_name"], "mac": mac, "sink": current}

    async def forget(self, mac: Optional[str]) -> Dict[str, Any]:
        mac = (mac or self.state.get("bt_mac") or "").upper()
        if not mac:
            raise BtError("bt_not_found", "אין רמקול שמור.")
        async with self.lock:
            if await self.bt_service_ok():
                await self.btctl(["disconnect", mac], timeout=8)
                await self.btctl(["remove", mac], timeout=8)
            if self.state.get("bt_mac") == mac:
                self.state.pop("bt_mac", None)
                self.state.pop("bt_name", None)
                if self.mode == "bluetooth":
                    self.state["mode"] = "auto"
                self._save()
                self.connected, self.connected_since = False, None
            if self.mode:
                await self.apply_output()
        self._changed()
        return {"removed": mac}

    # ------------------------------------------------------------ output / volume / tone

    async def set_output(self, mode: str) -> Dict[str, Any]:
        async with self.lock:
            self.state["mode"] = mode
            self.state.setdefault("sink_volume", DEFAULT_SINK_VOLUME)
            self._save()
            await self.tick()
        await self._disable_legacy_service()
        self._changed()
        return {"mode": mode, "sink": self.default_sink, "kind": sink_kind(self.default_sink)}

    async def set_volume(self, volume: int) -> Dict[str, Any]:
        """The system (sink) level; the player's own volume stays with volume_control."""
        async with self.lock:
            result = await run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{volume}%"], timeout=5)
            if not result.ok:
                raise BtError("audio_unavailable", "שרת השמע לא ענה.", result.text)
            self.state["sink_volume"] = volume
            self._save()
            self.sink_volume = volume
            self._volume_set_on = self.default_sink
        self._changed()
        return {"volume": volume, "sink": self.default_sink}

    async def test_tone(self) -> Dict[str, Any]:
        path = Path("/tmp/soundops_test_tone_1k.wav")
        if not path.exists():
            _write_tone(path)
        sink = await self.get_default_sink()
        argv = ["paplay", str(path)] + ([f"--device={sink}"] if sink else [])
        result = await run(argv, timeout=8)
        if not result.ok:
            raise BtError("audio_unavailable", "לא הצלחתי להשמיע צליל.", result.text)
        return {"sink": sink, "kind": sink_kind(sink)}

    # ------------------------------------------------------------ report

    def snapshot(self) -> Dict[str, Any]:
        return {
            "bluetooth": {
                "adapter_powered": self.adapter_powered,
                "speaker_mac": self.state.get("bt_mac"),
                "speaker_name": self.state.get("bt_name"),
                "connected": self.connected,
                "connected_since": int(self.connected_since) if self.connected_since else None,
                "last_error": self.last_error,
            },
            "audio": {
                "mode": self.mode,
                "default_sink": self.default_sink,
                "sink_kind": sink_kind(self.default_sink),
                "sink_volume": self.sink_volume,
                "sink_muted": self.sink_muted,
            },
        }


def _write_tone(path: Path, seconds: float = 2.0, freq: float = 1000.0) -> None:
    rate = 44100
    frames = bytearray()
    total = int(rate * seconds)
    for i in range(total):
        # Soft start and end, at about a third of full scale
        envelope = min(1.0, i / 2000, (total - i) / 2000)
        value = int(11000 * envelope * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<hh", value, value)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
