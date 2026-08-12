#!/usr/bin/env python3
"""Report audible dropouts at the Pi's speaker output, with wall-clock stamps.

Reads the default sink's monitor, so it measures what actually leaves the card.
This is the only trustworthy way to judge stutter: listen-pi-audio.sh drifts
behind real time and produces local underruns of its own, which sound identical
to a dropout on the Pi but are not one.

A loudness threshold alone cannot tell a starved output from a song fading out,
so each gap is also judged by how much of it was *digital* silence (all samples
zero, which only happens when nothing is being fed or the volume is 0):

    NO-DATA      mostly digital silence -> genuine dropout / muted stretch
    quiet-audio  real low-level samples -> fade-out, quiet intro, soft passage

Only NO-DATA gaps are counted as dropouts. Judging by the loudest sample in the
gap is not enough: a single frame at the boundary then hides a long silence.

KNOWN BLIND SPOT: when no stream is attached at all, PulseAudio suspends the
sink and the monitor stops producing data instead of producing zeros, so this
tool cannot see that kind of gap. Read the gap between "Using prefetched URL"
and "Playing track (buffer ready)" in the agent log for those.

Read-only: it opens a monitor *source* and never touches routing or volume.

    python3 scripts/audio-dropouts.py [seconds]
"""
import array
import os
import subprocess
import sys
import time
from datetime import datetime

os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

RATE = 44100
FRAME_MS = 10
# A peak of 300 (~-40 dBFS) sounded safe but flagged ordinary quiet passages and
# fade-outs as gaps, which is what produced a batch of phantom "dropouts".
SILENCE_PEAK = 60
# Choppy playback is made of gaps well under 100ms, and a 120ms floor hid them
# entirely: an ad that sounded like two seconds of stutter reported only 480ms.
MIN_GAP_S = 0.03
# Gaps closer together than this are reported as one episode of choppiness.
BURST_JOIN_S = 1.0

duration_s = float(sys.argv[1]) if len(sys.argv) > 1 else 900.0


def stamp(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


def default_monitor():
    sink = subprocess.run(
        ["pactl", "get-default-sink"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"{sink}.monitor"


monitor = default_monitor()
print(f"monitoring {monitor} for {duration_s:.0f}s "
      f"(gap threshold {MIN_GAP_S * 1000:.0f}ms)", flush=True)

proc = subprocess.Popen(
    ["parec", "-d", monitor, "--format=s16le", f"--rate={RATE}",
     "--channels=2", f"--latency-msec={FRAME_MS}"],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)

chunk = RATE // (1000 // FRAME_MS) * 4
deadline = time.monotonic() + duration_s
silent_since = None
was_loud = False
gap_peak = gap_frames = gap_dead = 0
loud_frames = total_frames = dropouts = 0
next_summary = time.monotonic() + 60.0
burst = None


def flush_burst():
    """Print one line per episode of choppiness rather than per tiny gap."""
    global burst
    if burst is None:
        return
    span = burst["last_end"] - burst["start"]
    if burst["count"] == 1:
        print(f"{burst['kind']:<11} {stamp(burst['start'])}  "
              f"{burst['silence'] * 1000:6.0f}ms  peak={burst['peak']:5d}", flush=True)
    else:
        print(f"{burst['kind']:<11} {stamp(burst['start'])}  "
              f"{burst['count']:3d} gaps over {span:5.1f}s, "
              f"silent {burst['silence'] * 1000:6.0f}ms  peak={burst['peak']:5d}",
              flush=True)
    burst = None


def add_gap(start, end, gap, kind, peak):
    global burst
    if burst is not None and (burst["kind"] != kind
                              or start - burst["last_end"] > BURST_JOIN_S):
        flush_burst()
    if burst is None:
        burst = {"start": start, "count": 0, "silence": 0.0,
                 "last_end": end, "kind": kind, "peak": peak}
    burst["count"] += 1
    burst["silence"] += gap
    burst["last_end"] = end
    burst["peak"] = max(burst["peak"], peak)


try:
    while time.monotonic() < deadline:
        data = proc.stdout.read(chunk)
        if not data:
            break
        samples = array.array("h", data[: len(data) // 2 * 2])
        peak = max((abs(s) for s in samples), default=0)
        now = time.time()
        total_frames += 1

        if peak > SILENCE_PEAK:
            loud_frames += 1
            if burst is not None and now - burst["last_end"] > BURST_JOIN_S:
                flush_burst()
            if silent_since is not None and was_loud:
                gap = now - silent_since
                dead = gap_dead / max(gap_frames, 1)
                starved = dead >= 0.5
                if gap >= MIN_GAP_S:
                    if starved:
                        dropouts += 1
                    add_gap(silent_since, now, gap,
                            "NO-DATA" if starved else "quiet-audio", gap_peak)
            silent_since = None
            gap_peak = gap_frames = gap_dead = 0
            was_loud = True
        else:
            if silent_since is None:
                silent_since = now
                gap_peak = gap_frames = gap_dead = 0
            gap_peak = max(gap_peak, peak)
            gap_frames += 1
            if peak <= 2:
                gap_dead += 1

        if time.monotonic() >= next_summary:
            next_summary += 60.0
            flush_burst()
            pct = 100.0 * loud_frames / max(total_frames, 1)
            print(f"-- {stamp(now)} audio present {pct:5.1f}% of last minute, "
                  f"real dropouts so far {dropouts}", flush=True)
            loud_frames = total_frames = 0
finally:
    flush_burst()
    proc.kill()
    print(f"done, {dropouts} real dropout(s) detected "
          f"(NO-DATA gaps only; quiet-audio lines are the music itself)", flush=True)
