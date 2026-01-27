import asyncio
import json
import logging
import platform
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from agent import Agent, AgentConfig, DeviceConfig, ServerClient


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
    if not data.get("devices"):
        raise ValueError("config.json must include a non-empty devices list")

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


async def register_device_with_retry(
    api_url: str,
    device: DeviceConfig,
    hardware_id: str,
) -> Optional[Tuple[str, str, int]]:
    resolved_hardware_id = hardware_id or device.device_id or device.device_name or f"branch-{device.branch_id}"
    client = ServerClient(api_url=api_url, device_token="", device_id=device.device_id or resolved_hardware_id)
    while True:
        response = await client.register_device(
            hardware_id=resolved_hardware_id,
            device_name=device.device_name,
            branch_id=device.branch_id,
            device_id=device.device_id or resolved_hardware_id,
        )
        if not response:
            logging.warning("Device registration failed. Retrying in 30s.")
            await asyncio.sleep(30)
            continue
        status = str(response.get("status", "")).lower()
        if status == "pending":
            logging.info("Device registration pending. Retrying in 30s.")
            await asyncio.sleep(30)
            continue
        token = response.get("device_token") or response.get("token")
        if token:
            resolved_device_id = str(response.get("device_id") or device.device_id or "").strip()
            resolved_branch_id = int(response.get("branch_id") or device.branch_id or 0)
            return str(token).strip(), resolved_device_id, resolved_branch_id
        logging.warning("Device registration returned no token. Retrying in 30s.")
        await asyncio.sleep(30)


def run_device(agent_config: AgentConfig, device: DeviceConfig) -> None:
    asyncio.run(Agent(agent_config, device).start())


def main() -> None:
    configure_logging()

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

    updated_devices: List[DeviceConfig] = []
    for device in config.devices:
        device_token = device.device_token
        if not device_token:
            logging.info(
                "No device_token for device_id=%s, attempting registration",
                device.device_id or device.device_name or device.branch_id,
            )
            registration = asyncio.run(register_device_with_retry(config.api_url, device, hardware_id))
            if registration:
                device_token, resolved_device_id, resolved_branch_id = registration
                persist_device_token(
                    config_path,
                    device,
                    device_token,
                    resolved_device_id,
                    resolved_branch_id,
                    config.api_url,
                    config.ws_url,
                )
                device = DeviceConfig(
                    device_id=resolved_device_id or device.device_id,
                    device_name=device.device_name,
                    branch_id=resolved_branch_id or device.branch_id,
                    device_token=device_token,
                )
        updated_devices.append(
            DeviceConfig(
                device_id=device.device_id,
                device_name=device.device_name,
                branch_id=device.branch_id,
                device_token=device_token,
            )
        )

    config = AgentConfig(api_url=config.api_url, ws_url=config.ws_url, devices=updated_devices)

    threads: List[threading.Thread] = []
    for device in config.devices:
        thread = threading.Thread(
            target=run_device,
            args=(config, device),
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
