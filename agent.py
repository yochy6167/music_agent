import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from player import MusicPlayer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    device_name: str
    branch_id: int
    device_token: str


@dataclass(frozen=True)
class AgentConfig:
    api_url: str
    ws_url: str
    devices: List[DeviceConfig]


class ServerClient:
    def __init__(self, api_url: str, device_token: str, device_id: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.device_token = device_token
        self.device_id = device_id
        self.headers = {
            "X-Device-Token": device_token,
            "Content-Type": "application/json",
        }

    async def send_heartbeat(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}/api/v1/devices/heartbeat"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=data, headers=self.headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            detail = None
            if response is not None:
                try:
                    detail = response.json()
                except ValueError:
                    detail = response.text
            logger.error(
                "Heartbeat error: %s | response=%s | payload=%s",
                exc,
                detail,
                data,
            )
            return None
        except httpx.HTTPError as exc:
            logger.error("Heartbeat error: %s", exc)
            return None

    async def register_device(
        self,
        hardware_id: str,
        device_name: str,
        branch_id: int,
        device_id: str,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}/api/v1/devices/register"
        payload = {
            "hardware_id": hardware_id,
            "device_name": device_name,
            "branch_id": branch_id,
            "device_id": device_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("Register device error: %s", exc)
            return None

    async def get_commands_long_polling(
        self,
        timeout: int = 25,
        heartbeat_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}/api/v1/devices/commands?timeout={timeout}"
        try:
            async with httpx.AsyncClient(timeout=float(timeout + 3)) as client:
                if heartbeat_data:
                    response = await client.post(url, json=heartbeat_data, headers=self.headers)
                else:
                    response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            return {"status": "ok", "commands": []}
        except httpx.HTTPError as exc:
            logger.error("Long polling error: %s", exc)
            return None

    async def get_playlist(self, playlist_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}/api/v1/playlists/{playlist_id}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("Playlist fetch error: %s", exc)
            return None

    async def log_playback_event(self, event: Dict[str, Any]) -> bool:
        """Log playback analytics to server (playback_logs table)."""
        url = f"{self.api_url}/api/v1/playback/log"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=event, headers=self.headers)
                response.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            logger.warning("Playback log error: %s", exc)
            return False


class WebSocketClient:
    def __init__(self, ws_url: str, device_token: str, branch_id: int) -> None:
        if ws_url.startswith("http://"):
            ws_url = ws_url.replace("http://", "ws://", 1)
        elif ws_url.startswith("https://"):
            ws_url = ws_url.replace("https://", "wss://", 1)
        elif not ws_url.startswith(("ws://", "wss://")):
            ws_url = f"ws://{ws_url}"

        self.ws_url = ws_url.rstrip("/")
        self.device_token = device_token
        self.branch_id = branch_id
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self.on_message: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.on_connect: Optional[Callable[[], Any]] = None
        self.on_disconnect: Optional[Callable[[], Any]] = None
        self._running = False

    async def connect(self) -> None:
        if self.connected:
            return
        self._running = True
        await self._connect_loop()

    async def _connect_loop(self) -> None:
        reconnect_delay = 3
        while self._running:
            try:
                url = f"{self.ws_url}/ws/agent/{self.branch_id}?token={self.device_token}"
                logger.info("Connecting to WebSocket: %s", url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=10,
                ) as websocket:
                    self.websocket = websocket
                    self.connected = True
                    reconnect_delay = 3
                    if self.on_connect:
                        self.on_connect()
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            if self.on_message:
                                await self.on_message(data)
                        except json.JSONDecodeError:
                            logger.error("Invalid JSON message from WS")
                        except Exception as exc:
                            logger.error("WS message handler error: %s", exc)
            except ConnectionClosed:
                self.connected = False
                if self.on_disconnect:
                    self.on_disconnect()
                if self._running:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, 30)
            except (InvalidURI, OSError) as exc:
                self.connected = False
                if self.on_disconnect:
                    self.on_disconnect()
                logger.error("WS connection error: %s", exc)
                if self._running:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, 30)
            except Exception as exc:
                self.connected = False
                if self.on_disconnect:
                    self.on_disconnect()
                logger.error("WS unexpected error: %s", exc)
                if self._running:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 1.5, 30)

    async def send(self, data: Dict[str, Any]) -> bool:
        if not self.connected or not self.websocket:
            return False
        try:
            await self.websocket.send(json.dumps(data))
            return True
        except Exception as exc:
            logger.error("WS send error: %s", exc)
            self.connected = False
            return False

    async def disconnect(self) -> None:
        self._running = False
        self.connected = False
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None


class Agent:
    def __init__(self, config: AgentConfig, device: DeviceConfig) -> None:
        self.config = config
        self.device = device
        self.client = ServerClient(
            api_url=config.api_url,
            device_token=device.device_token,
            device_id=device.device_id,
        )
        self.player = MusicPlayer(
            api_url=config.api_url,
            device_token=device.device_token,
        )
        self.ws_client: Optional[WebSocketClient] = None
        self.running = True
        self.is_connected = False
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.player.set_event_loop(loop)
        self.player.on_track_ended = self._log_track_playback

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            if self.config.ws_url:
                await self._start_websocket_with_retries()
            else:
                await self._long_polling_loop()
        finally:
            self.running = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _start_websocket_with_retries(self) -> None:
        reconnect_delay = 5
        while self.running:
            try:
                await self._start_websocket()
                return
            except Exception as exc:
                logger.warning("WS failed (%s). Retrying in %ss", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 60)

    async def _start_websocket(self) -> None:
        if not self.config.ws_url or not self.device.device_token:
            raise RuntimeError("WS_URL and device_token required for WebSocket")

        self.ws_client = WebSocketClient(
            ws_url=self.config.ws_url,
            device_token=self.device.device_token,
            branch_id=self.device.branch_id,
        )

        async def handle_ws_message(data: Dict[str, Any]) -> None:
            message_type = data.get("type")
            if message_type == "pending_commands":
                commands = data.get("commands", [])
                if commands:
                    await self._handle_commands(commands)
            elif message_type in {"playback_control", "volume_control"}:
                await self._handle_command(data)

        def on_connect() -> None:
            self.is_connected = True
            logger.info("WS connected for device %s", self.device.device_id)

        def on_disconnect() -> None:
            self.is_connected = False
            logger.warning("WS disconnected for device %s", self.device.device_id)

        self.ws_client.on_message = handle_ws_message
        self.ws_client.on_connect = on_connect
        self.ws_client.on_disconnect = on_disconnect

        ws_task = asyncio.create_task(self.ws_client.connect())
        await asyncio.sleep(3)
        if not self.ws_client.connected:
            ws_task.cancel()
            raise RuntimeError("WS connection failed to establish")

        while self.running:
            await asyncio.sleep(30)
            if self.ws_client and self.ws_client.connected:
                # Get full status including playback_position/playback_length
                status = await self.player.get_status()
                await self.ws_client.send(
                    {
                        "type": "status_update",
                        "current_volume": status.get("volume", 50.0),
                        "is_playing": status.get("is_playing", False),
                        "current_track_id": status.get("current_track_id"),
                        "current_playlist_id": status.get("current_playlist_id"),
                        "current_track": status.get("current_track"),
                        "track_position": status.get("track_position"),
                        "playback_position": status.get("playback_position"),
                        "playback_length": status.get("playback_length"),
                    }
                )
            else:
                break

        if self.ws_client:
            await self.ws_client.disconnect()
        raise RuntimeError("WS connection lost")

    async def _long_polling_loop(self) -> None:
        self.is_connected = True
        while self.running:
            status = await self.player.get_status()
            heartbeat_data = self._build_heartbeat(status)
            response = await self.client.get_commands_long_polling(
                timeout=25,
                heartbeat_data=heartbeat_data,
            )
            if not response:
                await asyncio.sleep(2)
                continue
            if response.get("repeat_mode"):
                self.player.repeat_mode = response["repeat_mode"]
            commands = response.get("commands") or []
            if commands:
                await self._handle_commands(commands)

    async def _handle_commands(self, commands: list) -> None:
        for command in commands:
            await self._handle_command(command)

    async def _handle_command(self, command: dict) -> None:
        command_type = command.get("type")
        action = command.get("action")
        action_normalized = (action or "").upper().replace("-", "_")
        try:
            # Dashboard sends action=update_software (admin API)
            if action_normalized == "UPDATE_SOFTWARE":
                await self._handle_update_software(command)
                return
            if command_type == "playback_control":
                await self._handle_playback_control(command)
            elif command_type == "volume_control":
                await self._handle_volume_control(command)
            else:
                logger.warning("Unknown command type: %s", command_type)
        except Exception as exc:
            logger.error("Command error (%s %s): %s", command_type, action, exc)

    async def _handle_playback_control(self, command: dict) -> None:
        action = command.get("action")
        if "repeat_mode" in command:
            self.player.repeat_mode = command.get("repeat_mode") or self.player.repeat_mode

        if action == "play":
            await self.player.play(
                playlist_id=command.get("playlist_id"),
                track_id=command.get("track_id"),
            )
        elif action == "pause":
            await self.player.pause()
        elif action == "stop":
            status = await self.player.get_status()
            track_id = status.get("current_track_id")
            if track_id:
                await self._log_track_playback(
                    track_id=int(track_id),
                    playlist_id=status.get("current_playlist_id"),
                    duration_played=float(status.get("playback_position") or 0.0),
                )
            await self.player.stop()
        elif action in {"skip", "next"}:
            await self.player.next()
        elif action == "previous":
            await self.player.previous()
        elif action == "seek":
            position = command.get("position") or command.get("seek_position")
            if position is not None:
                await self.player.seek(position)

    async def _handle_volume_control(self, command: dict) -> None:
        volume = command.get("volume")
        if volume is not None:
            await self.player.set_volume(volume)

    async def _handle_update_software(self, command: dict) -> None:
        logger.info("Starting remote update via Git...")
        await self._send_update_notice("Starting remote update via Git...")
        await self._send_ws_log("Pulling latest code...")

        git_process = await asyncio.create_subprocess_shell(
            "git pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        async def read_stream(stream: Optional[asyncio.StreamReader], collector: List[str], is_error: bool) -> None:
            if not stream:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                collector.append(text)
                if is_error:
                    logger.error(text)
                else:
                    logger.info(text)

        await asyncio.gather(
            read_stream(git_process.stdout, stdout_lines, False),
            read_stream(git_process.stderr, stderr_lines, True),
        )
        await git_process.wait()

        if git_process.returncode != 0:
            error_output = "\n".join(stderr_lines or stdout_lines).strip()
            logger.error("Git pull failed (%s): %s", git_process.returncode, error_output)
            await self._send_update_notice("Remote update failed during git pull.")
            await self._send_ws_log("Git pull failed. Update aborted.")
            return

        service_mode = bool(os.environ.get("INVOCATION_ID"))
        if not service_mode:
            logger.info("Update complete (Manual mode). Please restart the agent to apply changes.")
            await self._send_update_notice(
                "Update complete (Manual mode). Please restart the agent to apply changes.",
            )
            return

        await self._send_update_notice("Restarting...")
        await asyncio.sleep(2)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as exc:
            logger.warning("Exec restart failed: %s", exc)
            os._exit(0)

    async def _send_update_notice(self, message: str) -> None:
        try:
            status = await self.player.get_status()
            heartbeat = self._build_heartbeat(status)
            heartbeat["status_message"] = message
            heartbeat["update_in_progress"] = True
            await self.client.send_heartbeat(heartbeat)
        except Exception as exc:
            logger.warning("Update notice heartbeat failed: %s", exc)

        if self.ws_client and self.ws_client.connected:
            await self.ws_client.send(
                {
                    "type": "agent_log",
                    "message": message,
                    "level": "info",
                }
            )

    async def _send_ws_log(self, message: str) -> None:
        if self.ws_client and self.ws_client.connected:
            await self.ws_client.send(
                {
                    "type": "log",
                    "message": message,
                }
            )

    async def _log_track_playback(
        self,
        track_id: int,
        playlist_id: Optional[int],
        duration_played: float,
    ) -> None:
        """Persist completed track playback for dashboard analytics."""
        if duration_played < 1.0:
            return
        await self.client.log_playback_event(
            {
                "event_type": "track_ended",
                "track_id": track_id,
                "playlist_id": playlist_id,
                "duration_played": round(duration_played, 2),
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def _heartbeat_loop(self) -> None:
        while self.running:
            try:
                await asyncio.sleep(30)
                status = await self.player.get_status()
                await self.client.send_heartbeat(self._build_heartbeat(status))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Heartbeat loop error: %s", exc)

    def _build_heartbeat(self, status: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "healthy" if self.player.is_healthy() else "error",
            "current_volume": status.get("volume", 50.0),
            "is_playing": status.get("is_playing", False),
            "current_track_id": status.get("current_track_id"),
            "current_playlist_id": status.get("current_playlist_id"),
            "current_track": status.get("current_track"),
            "track_position": status.get("track_position"),
            "playback_position": status.get("playback_position"),
            "playback_length": status.get("playback_length"),
            "capabilities": self.player.get_capabilities(),
            "version": "1.0.0",
        }
