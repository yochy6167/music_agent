import asyncio
import json
import logging
import os
import platform
import secrets
import socket
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent import Agent, AgentConfig, DeviceConfig, ServerClient, agent_version
from device_ops import OpError, identify_led, restore_led
from health import image_version, local_ip
from oscmd import STATE_DIR, SYSTEM_DIR, write_json_atomic

_config_write_lock = threading.Lock()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def parse_config(data: dict) -> AgentConfig:
    devices_data = data.get("devices", [])

    devices: List[DeviceConfig] = []
    for device in devices_data:
        devices.append(
            DeviceConfig(
                device_id=str(device.get("device_id", "")).strip(),
                device_name=str(device.get("device_name", "")).strip(),
                branch_id=int(device.get("branch_id", 0)),
                device_token=str(device.get("device_token", "")).strip(),
            )
        )

    return AgentConfig(
        api_url=str(data.get("api_url", "")).strip(),
        ws_url=str(data.get("ws_url", "")).strip(),
        devices=devices,
    )


def load_or_init_config(config_path: Path) -> AgentConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"config.json not found at {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    for device in data.get("devices", []):
        if not device.get("device_name"):
            device["device_name"] = platform.node()

    return parse_config(data)


def get_rpi_serial() -> str:
    cpuinfo_path = Path("/proc/cpuinfo")
    if not cpuinfo_path.exists():
        return ""
    for line in cpuinfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.lower().startswith("serial"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return ""


def persist_device_token(
    config_path: Path,
    device: DeviceConfig,
    token: str,
    resolved_device_id: str,
    resolved_branch_id: int,
    api_url: str,
    ws_url: str,
) -> None:
    with _config_write_lock:
        _persist_device_token_unlocked(
            config_path,
            device,
            token,
            resolved_device_id,
            resolved_branch_id,
            api_url,
            ws_url,
        )


def _persist_device_token_unlocked(
    config_path: Path,
    device: DeviceConfig,
    token: str,
    resolved_device_id: str,
    resolved_branch_id: int,
    api_url: str,
    ws_url: str,
) -> None:
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        devices_data = data.get("devices", [])
    else:
        data = {"api_url": api_url, "ws_url": ws_url, "devices": []}
        devices_data = data["devices"]
    for entry in devices_data:
        entry_id = str(entry.get("device_id", "")).strip()
        entry_name = str(entry.get("device_name", "")).strip()
        entry_branch = int(entry.get("branch_id", 0))
        if device.device_id and entry_id == device.device_id:
            entry["device_token"] = token
            if resolved_device_id:
                entry["device_id"] = resolved_device_id
            if resolved_branch_id:
                entry["branch_id"] = resolved_branch_id
            break
        if not device.device_id and entry_name == device.device_name and entry_branch == device.branch_id:
            entry["device_token"] = token
            if resolved_device_id:
                entry["device_id"] = resolved_device_id
            if resolved_branch_id:
                entry["branch_id"] = resolved_branch_id
            break
    else:
        devices_data.append(
            {
                "device_id": resolved_device_id or device.device_id,
                "device_name": device.device_name,
                "branch_id": resolved_branch_id or device.branch_id,
                "device_token": token,
            }
        )
    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def persist_device_entry(
    config_path: Path,
    device: DeviceConfig,
    api_url: str,
    ws_url: str,
) -> None:
    with _config_write_lock:
        _persist_device_entry_unlocked(config_path, device, api_url, ws_url)


def _persist_device_entry_unlocked(
    config_path: Path,
    device: DeviceConfig,
    api_url: str,
    ws_url: str,
) -> None:
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        devices_data = data.get("devices", [])
    else:
        data = {"api_url": api_url, "ws_url": ws_url, "devices": []}
        devices_data = data["devices"]

    for entry in devices_data:
        entry_id = str(entry.get("device_id", "")).strip()
        entry_name = str(entry.get("device_name", "")).strip()
        entry_branch = int(entry.get("branch_id", 0))
        if device.device_id and entry_id == device.device_id:
            entry["device_name"] = device.device_name
            entry["branch_id"] = device.branch_id
            break
        if not device.device_id and entry_name == device.device_name and entry_branch == device.branch_id:
            entry["device_name"] = device.device_name
            break
    else:
        devices_data.append(
            {
                "device_id": device.device_id,
                "device_name": device.device_name,
                "branch_id": device.branch_id,
                "device_token": device.device_token,
            }
        )

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


# Waiting for approval: ask this often (the dashboard shows "assigned" within that time)
PENDING_POLL_S = 10
RETRY_S = 30
# What this agent can do beyond playing; the server shows such a Pi as remotely manageable
FEATURES = ["device_ops_v1"]


def claim_secret() -> str:
    """A secret made on this Pi: after approval the token goes only to whoever shows it.

    The SD image writes /etc/soundops/claim_secret on first boot; a Pi installed
    with setup.sh makes its own here on the first registration.
    """
    for path in (SYSTEM_DIR / "claim_secret", STATE_DIR / "claim_secret"):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(value) >= 16:
            return value
    value = secrets.token_urlsafe(32)
    path = STATE_DIR / "claim_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return value


def registration_details() -> Dict[str, Any]:
    return {
        "claim_secret": claim_secret(),
        "hostname": socket.gethostname()[:64],
        "local_ip": local_ip(),
        "image_version": image_version(),
        "agent_version": agent_version(),
        "features": FEATURES,
    }


async def run_pending_commands(commands: List[dict]) -> None:
    """An unassigned Pi gets only "identify" (blink the LED), through its register poll."""
    for command in commands or []:
        if command.get("action") != "identify":
            continue
        seconds = int((command.get("params") or {}).get("seconds") or 30)
        try:
            await identify_led(max(5, min(seconds, 120)))
            logging.info("Identify: blinking the LED for %ss", seconds)
        except OpError as exc:
            logging.warning("Identify failed: %s", exc.message)


async def register_device_with_retry(
    api_url: str,
    device: DeviceConfig,
    hardware_id: str,
) -> Optional[Tuple[str, str, int]]:
    resolved_hardware_id = hardware_id or device.device_id or device.device_name or f"branch-{device.branch_id}"
    client = ServerClient(api_url=api_url, device_token="", device_id=device.device_id or resolved_hardware_id)
    shown_code = None
    while True:
        response = await client.register_device(
            hardware_id=resolved_hardware_id,
            device_name=device.device_name,
            branch_id=device.branch_id,
            device_id=device.device_id or resolved_hardware_id,
            extra=registration_details(),
        )
        if not response:
            logging.warning("Device registration failed (no answer from the server). Retrying in %ss.", RETRY_S)
            await asyncio.sleep(RETRY_S)
            continue
        status = str(response.get("status", "")).lower()
        if status == "error":
            detail = response.get("detail")
            if detail == "Waiting for approval":
                # Older servers answer a pending device with 403
                logging.info("Waiting for approval in the dashboard. Asking again in %ss.", PENDING_POLL_S)
                await asyncio.sleep(PENDING_POLL_S)
            else:
                logging.warning("The server refused registration (%s: %s). Retrying in %ss.",
                                response.get("http_status"), detail, RETRY_S)
                await asyncio.sleep(RETRY_S)
            continue
        if status == "pending":
            code = response.get("pairing_code")
            if code:
                write_json_atomic(STATE_DIR / "pairing.json", {"pairing_code": code, "hardware_id": resolved_hardware_id})
                if code != shown_code:
                    logging.info("Waiting for approval in the dashboard. Pairing code: %s", code)
                    shown_code = code
            else:
                logging.info("Device registration pending. Retrying in %ss.", PENDING_POLL_S)
            await run_pending_commands(response.get("commands") or [])
            await asyncio.sleep(PENDING_POLL_S)
            continue
        token = response.get("device_token") or response.get("token")
        if token:
            # Assigned while "identify" was blinking: this loop ends, so hand the LED back now
            await restore_led()
            resolved_device_id = str(response.get("device_id") or device.device_id or "").strip()
            resolved_branch_id = int(response.get("branch_id") or device.branch_id or 0)
            return str(token).strip(), resolved_device_id, resolved_branch_id
        if status == "claimed":
            logging.warning(
                "Device token for hardware_id=%s was already issued. "
                "Release the device in the dashboard (or approve it again) to get a new one.",
                resolved_hardware_id,
            )
            await asyncio.sleep(RETRY_S)
            continue
        logging.warning("Device registration returned no token. Retrying in %ss.", RETRY_S)
        await asyncio.sleep(RETRY_S)


def run_device(agent_config: AgentConfig, device: DeviceConfig) -> None:
    asyncio.run(Agent(agent_config, device).start())


def run_device_thread(
    agent_config: AgentConfig,
    device: DeviceConfig,
    config_path: Path,
    hardware_id: str,
) -> None:
    """Register this device if needed, then run it.

    Each device has its own thread so one device stuck on admin approval
    does not keep the others from starting.
    """
    if not device.device_token:
        logging.info(
            "No device_token for device_id=%s, attempting registration",
            device.device_id or device.device_name or device.branch_id,
        )
        registration = asyncio.run(
            register_device_with_retry(agent_config.api_url, device, hardware_id)
        )
        if not registration:
            logging.error(
                "Registration ended without a token for device_id=%s",
                device.device_id or device.device_name or device.branch_id,
            )
            return
        device_token, resolved_device_id, resolved_branch_id = registration
        persist_device_token(
            config_path,
            device,
            device_token,
            resolved_device_id,
            resolved_branch_id,
            agent_config.api_url,
            agent_config.ws_url,
        )
        device = DeviceConfig(
            device_id=resolved_device_id or device.device_id,
            device_name=device.device_name,
            branch_id=resolved_branch_id or device.branch_id,
            device_token=device_token,
        )
    run_device(agent_config, device)


def main() -> None:
    configure_logging()
    print(f"--- Version {agent_version()} ---")
    base_dir = Path(__file__).resolve().parent
    candidate_paths = [
        base_dir / "config.json",
        Path.cwd() / "config.json",
        base_dir / "agent" / "config.json",
    ]
    config_path = next((path for path in candidate_paths if path.exists()), None)
    if config_path is None:
        tried = ", ".join(str(path) for path in candidate_paths)
        raise FileNotFoundError(f"config.json not found. Tried: {tried}")

    config = load_or_init_config(config_path)
    logging.info("Loaded config for %d device(s)", len(config.devices))

    hardware_id = get_rpi_serial()
    if not hardware_id:
        logging.warning("RPi serial not found; using fallback hardware id")

    if not config.devices:
        device_name = platform.node()
        default_device = DeviceConfig(
            device_id=hardware_id,
            device_name=device_name,
            branch_id=0,
            device_token="",
        )
        persist_device_entry(
            config_path=config_path,
            device=default_device,
            api_url=config.api_url,
            ws_url=config.ws_url,
        )
        config = AgentConfig(api_url=config.api_url, ws_url=config.ws_url, devices=[default_device])
        logging.info("Initialized config with device_name=%s", device_name)

    threads: List[threading.Thread] = []
    for device in config.devices:
        thread = threading.Thread(
            target=run_device_thread,
            args=(config, device, config_path, hardware_id),
            name=f"agent-{device.device_id or device.device_name or device.branch_id}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()
        logging.info(
            "Started device thread: device_id=%s device_name=%s branch_id=%s",
            device.device_id,
            device.device_name,
            device.branch_id,
        )

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        logging.info("Shutdown requested by user")


if __name__ == "__main__":
    main()
