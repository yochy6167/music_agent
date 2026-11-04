"""How loud is what the player sends to its output, measured on the sink's monitor.

Two uses:
- a live meter for the dashboard (audio_meter): about 10 frames a second of
  level and 8 bands, only while someone has the device page open;
- the silence check: every 30 seconds, 3 seconds of level. Long silence while
  the branch should be playing is an alert (the server decides when).

Only levels leave the Pi, never the sound. The monitor is what PipeWire sends
to the speaker, not a microphone: a speaker turned down on its own knob still
counts as sound here.
"""

import array
import asyncio
import logging
import math
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from oscmd import tool_env

logger = logging.getLogger(__name__)

RATE = 16000
FRAME = 1600  # 100 ms
BAND_WINDOW = 256  # the newest 16 ms of each frame goes to the bands (cheap in pure Python)
BANDS_HZ = (63, 125, 250, 500, 1000, 2000, 4000, 6300)
# Three points across each octave: one narrow bin per band missed a tone between centers
_BAND_COEFFS = [
    [2 * math.cos(2 * math.pi * min(f * k, RATE / 2 - 1) / RATE) for k in (2 ** (-1 / 3), 1.0, 2 ** (1 / 3))]
    for f in BANDS_HZ
]
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (BAND_WINDOW - 1)) for i in range(BAND_WINDOW)]

# At full player volume, below this (dBFS) a frame is silence. The player's
# volume is cubic (music measured -19 dB at 80 and -50 dB at 20), so the
# threshold follows it down to -90 dB (true silence is -96); same rule as the
# server's alerts (device_monitor.silence_threshold_db).
SILENCE_DB = -50.0
SILENCE_FLOOR_DB = -90.0
SAMPLE_EVERY_S = 30
SAMPLE_SECONDS = 3
STREAM_MAX_S = 120

Send = Callable[[Dict[str, Any]], Awaitable[Any]]


def silence_threshold_db(volume: Optional[float]) -> float:
    v = 100.0 if volume is None else max(0.0, min(float(volume), 100.0))
    if v <= 0:
        return SILENCE_FLOOR_DB
    return max(SILENCE_FLOOR_DB, SILENCE_DB + 60.0 * math.log10(v / 100.0))


def analyse(samples: "array.array") -> Dict[str, Any]:
    """Level (dBFS), peak (0–1) and 8 bands of one frame.

    ``bands``: 0–1 over -60..0 dB (older dashboards); ``bands_db``: the plain dB,
    so a page can scale them to the frame (music at volume 20 sits under -60).
    """
    n = len(samples)
    if not n:
        return {"rms_db": -96.0, "peak": 0.0, "bands": [0.0] * len(BANDS_HZ), "bands_db": [-96.0] * len(BANDS_HZ)}
    power = sum(x * x for x in samples) / n
    rms = math.sqrt(power) / 32768
    peak = max(abs(max(samples)), abs(min(samples))) / 32768
    weights = _HANN if n >= BAND_WINDOW else [1.0] * n
    window = [x * w for x, w in zip(samples[-BAND_WINDOW:], weights)]
    scale = (len(window) / 4) * 32768  # a full-scale sine gives about 1 after the Hann window
    bands: List[float] = []
    bands_db: List[float] = []
    for coeffs in _BAND_COEFFS:
        best = 0.0
        for coeff in coeffs:
            s1 = s2 = 0.0
            for x in window:
                s0 = x + coeff * s1 - s2
                s2, s1 = s1, s0
            best = max(best, s1 * s1 + s2 * s2 - coeff * s1 * s2)
        magnitude = math.sqrt(best) / scale
        db = 20 * math.log10(magnitude) if magnitude > 0 else -96.0
        bands.append(round(min(1.0, max(0.0, (db + 60) / 60)), 3))
        bands_db.append(round(max(db, -96.0), 1))
    return {
        "rms_db": round(20 * math.log10(rms), 1) if rms > 0 else -96.0,
        "peak": round(min(peak, 1.0), 3),
        "bands": bands,
        "bands_db": bands_db,
    }


async def frames(sink: str, seconds: float) -> AsyncIterator["array.array"]:
    """100 ms frames of the sink's monitor, mono 16 kHz, for up to ``seconds``."""
    process = await asyncio.create_subprocess_exec(
        "pacat", "--record", "--raw", "-d", f"{sink}.monitor",
        "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=100",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=tool_env(),
    )
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(process.stdout.readexactly(FRAME * 2), timeout=3)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                return
            frame = array.array("h")
            frame.frombytes(raw)
            yield frame
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class LevelMonitor:
    def __init__(
        self,
        output: Any,
        on_change: Optional[Callable[[], None]] = None,
        volume: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        self.output = output
        self.on_change = on_change
        self._volume = volume or (lambda: None)
        self.silent_since: Optional[float] = None
        self.level_db: Optional[float] = None
        self.measured_at: Optional[float] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._stream_until = 0.0

    def snapshot(self) -> Dict[str, Any]:
        return {
            "silent_since": int(self.silent_since) if self.silent_since else None,
            "level_db": self.level_db,
            "level_at": int(self.measured_at) if self.measured_at else None,
        }

    def _note(self, loudest_db: float, started: float) -> None:
        """One measurement: silence starts at its beginning, any sound ends it."""
        # The monitor is taken before the sink's volume: a muted output still
        # "sounds" there, so mute and 0% count as silence here.
        if self.output.sink_muted or self.output.sink_volume == 0:
            loudest_db = -96.0
        was_silent = self.silent_since is not None
        self.level_db, self.measured_at = round(loudest_db, 1), time.time()
        if loudest_db < silence_threshold_db(self._volume()):
            self.silent_since = self.silent_since or started
        else:
            self.silent_since = None
        if was_silent != (self.silent_since is not None) and self.on_change:
            self.on_change()

    async def _sink(self) -> Optional[str]:
        return self.output.default_sink or await self.output.get_default_sink()

    async def start(self) -> None:
        asyncio.create_task(self._sampler())

    async def _sampler(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                # A running live meter measures already
                if time.monotonic() >= self._stream_until:
                    sink = await self._sink()
                    if sink:
                        started = time.time()
                        loudest = -96.0
                        async for frame in frames(sink, SAMPLE_SECONDS):
                            loudest = max(loudest, analyse_level(frame))
                        self._note(loudest, started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Silence sample failed: %s", exc)
            await asyncio.sleep(SAMPLE_EVERY_S)

    async def stream(self, seconds: int, send: Send) -> Dict[str, Any]:
        """Start (or extend) the live meter; frames go out through ``send``."""
        seconds = max(5, min(int(seconds), STREAM_MAX_S))
        sink = await self._sink()
        if not sink:
            raise RuntimeError("no output to measure")
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
        self._stream_until = time.monotonic() + seconds
        self._stream_task = asyncio.create_task(self._run_stream(sink, seconds, send))
        return {"streaming": seconds, "sink": sink}

    async def _run_stream(self, sink: str, seconds: int, send: Send) -> None:
        second_start, loudest = time.time(), -96.0
        try:
            async for frame in frames(sink, seconds):
                result = analyse(frame)
                loudest = max(loudest, result["rms_db"])
                await send({
                    "type": "audio_level", **result,
                    # The monitor is before the sink volume: say when nothing reaches the speaker
                    "muted": bool(self.output.sink_muted or self.output.sink_volume == 0),
                    "volume": self._volume(),
                })
                if time.time() - second_start >= SAMPLE_SECONDS:
                    self._note(loudest, second_start)
                    second_start, loudest = time.time(), -96.0
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Live audio meter stopped: %s", exc)
        finally:
            # A newer stream replaced this one: its deadline stands
            if self._stream_task is asyncio.current_task():
                self._stream_until = 0.0


def analyse_level(samples: "array.array") -> float:
    """Level only (dBFS): the silence check does not need the bands."""
    n = len(samples)
    if not n:
        return -96.0
    rms = math.sqrt(sum(x * x for x in samples) / n) / 32768
    return 20 * math.log10(rms) if rms > 0 else -96.0
