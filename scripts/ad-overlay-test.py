#!/usr/bin/env python3
"""Measure how cleanly an ad plays over music, using the agent's own player.

Ads overlap the music, and this Pi's sound card is easy to starve while two
streams are open, so a change that sounds fine for music alone can still make
every ad stutter. This drives the real MusicPlayer - same VLC options, same
yt-dlp resolution, same play_ad() path the dashboard triggers - and reports the
dead air measured at the card's own monitor, which is the only place the result
cannot be confused with network or listener lag.

Run it on the Pi, ideally with the service stopped so nothing else competes:

    sudo systemctl stop music_agent
    python3 scripts/ad-overlay-test.py            # 3 ads
    REPEATS=6 python3 scripts/ad-overlay-test.py  # 6 ads, for a noisy card
    sudo systemctl start music_agent

Healthy result on the reference Pi: under a second of dead air per ad, no upward
trend across repeats. A total that grows with each ad means the streams are
fighting - see "מודול פלט האודיו" in RPI_SETUP.md before changing anything.
"""
import array
import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

from player import MusicPlayer  # noqa: E402  (needs the path set above)

RATE = 44100
FRAME_MS = 10
MIN_GAP_MS = 30
# Loud enough to separate the ad from the ducked music underneath it.
AD_PEAK = 2000
REPEATS = int(os.environ.get("REPEATS", "3"))
# Any playable tracks; only the audio path is under test, not the content.
VIDEOS = sys.argv[1:] or ["kJQP7kiw5Fk", "9bZkp7q19f0", "OPf0YbXqDm0"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


def default_monitor():
    sink = subprocess.run(["pactl", "get-default-sink"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return f"{sink}.monitor"


def find_ad_audio():
    """Reuse a cached ad if the agent has one, else synthesise a stand-in.

    Returns the path plus its cache key, so play_ad() finds it already cached
    and does not try to download it from a backend that is not running here.
    """
    cache = Path.home() / ".soundops_agent" / "audio_cache"
    for f in sorted(cache.glob("ad_*.mp3")) if cache.is_dir() else []:
        if f.stat().st_size > 0:
            key = f.stem.split("_", 1)[1]
            return f, int(key) if key.isdigit() else None
    import math
    import wave
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "ad_0.mp3"          # not really an mp3; VLC probes content
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(array.array("h", [
            int(18000 * math.sin(2 * math.pi * 440.0 * n / 48000))
            for n in range(48000 * 8)
        ]).tobytes())
    return path, 0


class Monitor:
    """Records the card's output as one character per 10ms of real time."""

    def __init__(self, monitor):
        self.proc = subprocess.Popen(
            ["parec", "-d", monitor, "--format=s16le", f"--rate={RATE}",
             "--channels=2", f"--latency-msec={FRAME_MS}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.chunk = RATE // (1000 // FRAME_MS) * 4

    def flush(self):
        """Drop whatever buffered up while we were not reading.

        Without this the next recording silently starts with minutes-old audio,
        which turns a 6s window into an unreadable mix of past and present.
        """
        fd = self.proc.stdout.fileno()
        os.set_blocking(fd, False)
        try:
            while self.proc.stdout.read(1 << 16):
                pass
        except (BlockingIOError, TypeError):
            pass
        finally:
            os.set_blocking(fd, True)

    async def record(self, frames):
        loop = asyncio.get_running_loop()
        out = []
        while len(out) < frames:
            data = await loop.run_in_executor(None, self.proc.stdout.read, self.chunk)
            if not data:
                break
            samples = array.array("h", data[: len(data) // 2 * 2])
            peak = max((abs(s) for s in samples), default=0)
            out.append("#" if peak > AD_PEAK else ("." if peak > 2 else " "))
        return "".join(out)

    def close(self):
        self.proc.kill()


def dead_air(timeline):
    """Runs where the card produced no data at all, in milliseconds."""
    out, run = [], 0
    for ch in timeline + "x":
        if ch == " ":
            run += 1
            continue
        if run * FRAME_MS >= MIN_GAP_MS:
            out.append(run * FRAME_MS)
        run = 0
    return out


async def main():
    ad_audio, ad_media_id = find_ad_audio()
    player = MusicPlayer(api_url="http://127.0.0.1:1", device_token="offline-test")
    player._init_vlc()
    if not player.player:
        sys.exit("VLC did not initialise")
    player.volume = 50.0
    player.current_playlist_id = 0
    player.current_playlist = [
        {"id": i, "source": "youtube", "source_id": v, "title": v}
        for i, v in enumerate(VIDEOS)
    ]

    print(f"ad audio: {ad_audio}", flush=True)
    await player.play()
    for _ in range(40):
        await asyncio.sleep(0.5)
        if player.player.get_time() > 3000:
            break
    if player.player.get_time() <= 0:
        sys.exit("music never started - the result would be meaningless")

    monitor = Monitor(default_monitor())
    await asyncio.sleep(2.0)
    monitor.flush()
    print(f"music alone: {dead_air(await monitor.record(300)) or 'clean'}", flush=True)

    totals = []
    for i in range(REPEATS):
        await asyncio.sleep(3.0)
        monitor.flush()
        recording = asyncio.create_task(monitor.record(600))
        await asyncio.sleep(0.3)
        await player.play_ad(
            f"file://{ad_audio}", campaign_id=0, audio_media_id=ad_media_id,
            play_type="interval_minutes",
            schedule_config={"overlay_mode": "duck", "duck_music_volume_percent": 25},
        )
        gaps = dead_air(await recording)
        totals.append(sum(gaps))
        print(f"ad {i + 1}: {sum(gaps):5d}ms dead air in {len(gaps):2d} gap(s)  {gaps}",
              flush=True)
        try:
            player.ad_player.stop()
        except Exception:
            pass
        player._ad_playing = False
        player.player.audio_set_volume(int(player.volume))

    monitor.close()
    await player.stop()
    trend = "steady" if totals[-1] <= max(totals[0], 500) else "GETTING WORSE"
    print(f"\ntotal {sum(totals)}ms over {REPEATS} ad(s), trend: {trend}", flush=True)


asyncio.run(main())
