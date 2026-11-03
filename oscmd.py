"""Running system tools from the agent: argv only (never a shell), always with a timeout.

A tool that hangs (bluetoothctl waits forever when bluetoothd is down) is killed
when its time is up, so no device action can wedge the agent.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-device state written by the agent (audio output, pairing code, a pending update)
STATE_DIR = Path.home() / ".config" / "soundops"
# Written once on first boot by the SD image (device.json, claim_secret); absent on setup.sh installs
SYSTEM_DIR = Path("/etc/soundops")


@dataclass
class Result:
    code: int
    out: str
    err: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()


def tool_env() -> Dict[str, str]:
    """English output (parsed below) and the user's PipeWire session, even when started oddly."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if not env.get("XDG_RUNTIME_DIR"):
        runtime = Path(f"/run/user/{os.getuid()}")
        if runtime.is_dir():
            env["XDG_RUNTIME_DIR"] = str(runtime)
    return env


async def run(argv: List[str], timeout: float = 10.0, input_text: Optional[str] = None) -> Result:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=tool_env(),
        )
    except FileNotFoundError:
        return Result(127, "", f"{argv[0]}: not found")
    except OSError as exc:
        return Result(126, "", str(exc))
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_text.encode() if input_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
        except (asyncio.TimeoutError, ValueError):
            stdout, stderr = b"", b""
        return Result(-9, stdout.decode(errors="replace"), stderr.decode(errors="replace"), timed_out=True)
    return Result(process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


async def run_sudo(argv: List[str], timeout: float = 10.0, input_text: Optional[str] = None) -> Result:
    """Root for the few fixed commands that need it; never asks for a password."""
    return await run(["sudo", "-n", *argv], timeout=timeout, input_text=input_text)


def needs_password(result: Result) -> bool:
    return "password is required" in result.err or "a terminal is required" in result.err


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
