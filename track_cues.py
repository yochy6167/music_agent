"""Silence trimming: where a song's music really starts and ends.

Many YouTube uploads open with a second or more of silence and end with
several more (measured on 10 songs: 1.4s at the start, 2.9s at the end on
average). That silence is inside the file, so even a gapless switch leaves a
hole between songs. Here the player measures it and then plays each song with
VLC's :start-time / :stop-time.

How, without ffmpeg (players don't have it; VLC is always installed):
- VLC decodes only the first 20s and the last 30s of the stream to an 8kHz
  mono WAV, niced, in its own process. Measured on a desktop: ~0.25s each
  (about 100x real time); a Pi 4 is several times slower, a few seconds in
  all, once per song, while the song before it plays.
- Pure Python finds the first and last 50ms window above -55 dBFS. The end is
  measured from the end of the decoded audio, because a seek in an m4a lands
  up to ~2s early (measured), and the exact length comes from the stream URL
  (``dur=`` parameter).
- Only real silence is cut: a quiet passage or a fade stays. When unsure
  (nothing loud in a section, no length), nothing is cut.

Results are kept locally and sent to the server, which shares them with all
players through the playlist (play_start_s / play_end_s).
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

VERSION = 1
HEAD_S = 20.0
TAIL_S = 30.0
RATE = 8000
WINDOW_S = 0.05
THRESHOLD_DB = -55.0
MIN_LEAD_S = 0.4  # shorter silences are not worth a cut
MIN_TAIL_S = 0.6
KEEP_BEFORE_S = 0.05  # start a hair before the first sound
KEEP_AFTER_S = 0.3  # let the last note ring out
DECODE_TIMEOUT_S = 45.0
_STORE_LIMIT = 5000


def vlc_binary() -> Optional[str]:
    override = os.environ.get("SOUNDOPS_VLC_BIN")
    if override:
        return override if Path(override).is_file() else None
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", "")):
            exe = Path(base) / "VideoLAN" / "VLC" / "vlc.exe"
            if base and exe.is_file():
                return str(exe)
        return shutil.which("vlc")
    return shutil.which("cvlc") or shutil.which("vlc")


def duration_from_url(url: str) -> Optional[float]:
    """googlevideo URLs carry the exact stream length: ...&dur=199.710&..."""
    try:
        value = parse_qs(urlparse(url).query).get("dur", [None])[0]
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _pcm_from_wav(path: Path) -> array.array:
    """16-bit samples after the "data" chunk header (VLC may leave its size unset)."""
    raw = path.read_bytes()
    at = raw.find(b"data")
    samples = array.array("h")
    if at < 0:
        return samples
    body = raw[at + 8:]
    samples.frombytes(body[: len(body) - len(body) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def window_levels(samples: array.array, rate: int = RATE, window_s: float = WINDOW_S) -> list:
    """dBFS (RMS) per window."""
    step = max(1, int(rate * window_s))
    out = []
    for i in range(0, len(samples) - step + 1, step):
        chunk = samples[i:i + step]
        energy = sum(x * x for x in chunk) / step
        out.append(20 * math.log10(math.sqrt(energy) / 32768) if energy > 0 else -120.0)
    return out


def find_cue(head: list, tail: list, duration_s: float) -> Optional[Dict[str, Any]]:
    """{start_s, end_s, lead_s, tail_s} from window levels, or None when unclear."""
    loud_head = next((i for i, v in enumerate(head) if v > THRESHOLD_DB), None)
    loud_tail = next((i for i in range(len(tail) - 1, -1, -1) if tail[i] > THRESHOLD_DB), None)
    if loud_head is None or loud_tail is None or duration_s <= HEAD_S + TAIL_S:
        return None
    lead = loud_head * WINDOW_S
    trailing = (len(tail) - loud_tail - 1) * WINDOW_S
    start_s = round(max(0.0, lead - KEEP_BEFORE_S), 2) if lead >= MIN_LEAD_S else 0.0
    end_s = round(duration_s - max(0.0, trailing - KEEP_AFTER_S), 2) if trailing >= MIN_TAIL_S else None
    return {"start_s": start_s, "end_s": end_s, "lead_s": round(lead, 2), "tail_s": round(trailing, 2)}


def _kill(proc: "asyncio.subprocess.Process") -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _decode(vlc: str, url: str, dst: Path, start: float, stop: Optional[float]) -> bool:
    cmd = []
    if os.name == "posix":
        # Lowest CPU and disk priority: the song that is playing must never stutter.
        if shutil.which("nice"):
            cmd += ["nice", "-n", "19"]
        if shutil.which("ionice"):
            cmd += ["ionice", "-c3"]
    cmd += [vlc, "-I", "dummy"]
    if os.name == "nt":
        cmd.append("--dummy-quiet")  # Windows only: Linux VLC rejects it and exits at once
    cmd += [
        # Audio only: some formats carry video, and transcoding it fails the mux.
        "--no-video", "--no-sout-video", "--no-sout-spu", "--no-sub-autodetect-file", "--no-stats",
        "--http-reconnect", f"--start-time={start:.2f}",
    ]
    if stop is not None:
        cmd.append(f"--stop-time={stop:.2f}")
    cmd += [
        url,
        f"--sout=#transcode{{acodec=s16l,channels=1,samplerate={RATE}}}:std{{access=file,mux=wav,dst={dst}}}",
        "vlc://quit",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=DECODE_TIMEOUT_S)
    except BaseException:
        _kill(proc)
        raise
    return dst.is_file() and dst.stat().st_size > 1000


async def analyze(url: str, duration_hint: Optional[float]) -> Optional[Dict[str, Any]]:
    """Measure a stream's silent edges. None when it could not be measured."""
    vlc = vlc_binary()
    duration = duration_from_url(url) or (float(duration_hint) if duration_hint else None)
    if not vlc or not duration:
        return None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="soundops-cue-") as tmp:
        head_wav, tail_wav = Path(tmp) / "head.wav", Path(tmp) / "tail.wav"
        try:
            ok_head = await _decode(vlc, url, head_wav, 0.0, HEAD_S)
            # No stop-time: decode to the real end, which is what the end is measured from.
            ok_tail = ok_head and await _decode(vlc, url, tail_wav, max(0.0, duration - TAIL_S), None)
        except asyncio.TimeoutError:
            logger.warning("Silence analysis timed out")
            return None
        if not (ok_head and ok_tail):
            return None
        # Plain Python over ~400k samples: a fraction of a second, off the event loop.
        head, tail = await asyncio.to_thread(
            lambda: (window_levels(_pcm_from_wav(head_wav)), window_levels(_pcm_from_wav(tail_wav)))
        )
    cue = find_cue(head, tail, duration)
    if cue is not None:
        cue["duration_s"] = round(duration, 2)
        cue["took_s"] = round(time.monotonic() - started, 2)
    return cue


class CueStore:
    """Cues this player measured or got from the server, by video id (JSON file)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, source_id: Optional[str]) -> Optional[Dict[str, Any]]:
        entry = self._data.get(source_id or "")
        return entry if entry and entry.get("v", 0) >= VERSION else None

    def put(self, source_id: str, start_s: float, end_s: Optional[float], duration_s: Optional[float]) -> None:
        self._data[source_id] = {"s": start_s, "e": end_s, "d": duration_s, "v": VERSION, "t": int(time.time())}
        if len(self._data) > _STORE_LIMIT:
            for key, _ in sorted(self._data.items(), key=lambda kv: kv[1].get("t", 0))[: len(self._data) - _STORE_LIMIT]:
                self._data.pop(key, None)
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Could not save silence cues: %s", exc)
