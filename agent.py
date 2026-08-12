import asyncio
import json
import logging
import os
import random
import sys
import time
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

    async def get_ad_transition_config(self) -> Optional[list]:
        url = f"{self.api_url}/api/v1/devices/ads/transition-config"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.warning("Failed to load ad transition config: %s", exc)
            return None


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
        self._outbound: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue(maxsize=100)
        self._sender_task: Optional[asyncio.Task] = None
        self._pending_status: Optional[Dict[str, Any]] = None

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
                    ping_timeout=60,
                    close_timeout=10,
                    max_queue=64,
                ) as websocket:
                    self.websocket = websocket
                    self.connected = True
                    reconnect_delay = 3
                    # Drain stale outbound messages from a previous connection.
                    while not self._outbound.empty():
                        try:
                            self._outbound.get_nowait()
                            self._outbound.task_done()
                        except asyncio.QueueEmpty:
                            break
                    self._sender_task = asyncio.create_task(self._sender_loop())
                    if self.on_connect:
                        self.on_connect()
                    try:
                        async for message in websocket:
                            try:
                                data = json.loads(message)
                            except json.JSONDecodeError:
                                logger.error("Invalid JSON message from WS")
                                continue
                            if self.on_message:
                                asyncio.create_task(self._run_message_handler(data))
                    finally:
                        if self._sender_task:
                            self._sender_task.cancel()
                            try:
                                await self._sender_task
                            except asyncio.CancelledError:
                                pass
                            self._sender_task = None
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

    async def _run_message_handler(self, data: Dict[str, Any]) -> None:
        try:
            await self.on_message(data)
        except Exception as exc:
            logger.error("WS message handler error: %s", exc)

    async def _sender_loop(self) -> None:
        """Single writer — prevents concurrent send deadlocks with WS pings."""
        while self._running and self.connected:
            try:
                data = await self._outbound.get()
            except asyncio.CancelledError:
                break
            if data is None:
                self._outbound.task_done()
                break
            try:
                if data.get("type") == "_flush_status":
                    data = self._pending_status
                    self._pending_status = None
                    if not data:
                        self._outbound.task_done()
                        continue
                if not self.websocket or not self.connected:
                    self._outbound.task_done()
                    break
                raw = json.dumps(data, ensure_ascii=False, default=str)
                await asyncio.wait_for(self.websocket.send(raw), timeout=8.0)
            except asyncio.TimeoutError:
                logger.error("WS send timed out — marking disconnected")
                self.connected = False
                self._outbound.task_done()
                try:
                    if self.websocket:
                        await self.websocket.close()
                except Exception:
                    pass
                break
            except asyncio.CancelledError:
                self._outbound.task_done()
                break
            except Exception as exc:
                logger.error("WS send error: %s", exc)
                self.connected = False
                self._outbound.task_done()
                break
            else:
                self._outbound.task_done()

    async def send(self, data: Dict[str, Any]) -> bool:
        if not self.connected:
            return False
        # Coalesce status updates: keep only the newest payload.
        if data.get("type") == "status_update":
            self._pending_status = data
            data = {"type": "_flush_status"}
        try:
            self._outbound.put_nowait(data)
            return True
        except asyncio.QueueFull:
            if data.get("type") == "_flush_status":
                # Status still stored in _pending_status; next flush will send it.
                return True
            logger.warning("WS outbound queue full — dropping message type=%s", data.get("type"))
            return False

    async def disconnect(self) -> None:
        self._running = False
        self.connected = False
        try:
            self._outbound.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._sender_task:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
            self._sender_task = None
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
        self._last_ws_payload: Optional[dict] = None
        self._transition_campaigns: list = []
        self._transition_pick_index = 0
        self._ad_config_refresh_counter = 0
        self._ws_status_ticks = 0
        self._command_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._control_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._ad_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._command_worker_task: Optional[asyncio.Task] = None
        self._control_worker_task: Optional[asyncio.Task] = None
        self._ad_worker_task: Optional[asyncio.Task] = None
        self._last_command_tick: float = 0.0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.player.set_event_loop(loop)
        self.player.on_track_ended = self._log_track_playback
        self.player.on_ad_transition_check = self._on_ad_transition_check
        self.player.on_ad_finished = self._on_ad_finished
        await self._refresh_transition_campaigns()

        self._command_worker_task = asyncio.create_task(self._command_worker())
        self._control_worker_task = asyncio.create_task(self._control_worker())
        self._ad_worker_task = asyncio.create_task(self._ad_worker())
        asyncio.create_task(self._watchdog_loop())

        # Always keep a light HTTP heartbeat as last_seen backup. WS status_update is
        # primary, but if the WS send path wedges the dashboard still sees the device.
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
            if self._command_worker_task:
                self._command_worker_task.cancel()
            if self._control_worker_task:
                self._control_worker_task.cancel()
            if self._ad_worker_task:
                self._ad_worker_task.cancel()

    def _is_priority_control(self, command: dict) -> bool:
        command_type = command.get("type")
        action = (command.get("action") or "").lower()
        if command_type == "volume_control":
            return True
        if command_type == "playback_control" and action in {
            "pause",
            "stop",
            "set_repeat_mode",
        }:
            return True
        return False

    def _is_ad_command(self, command: dict) -> bool:
        return command.get("type") == "ad_control"

    async def _control_worker(self) -> None:
        """Fast path for volume/pause/stop — never blocked behind yt-dlp/play."""
        logger.info("Control worker started")
        while self.running:
            try:
                command = await self._control_queue.get()
                self._last_command_tick = time.time()
                try:
                    logger.info(
                        "Handling control command: type=%s action=%s",
                        command.get("type"),
                        command.get("action"),
                    )
                    await asyncio.wait_for(self._handle_command(command), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.error("Control command timed out: %s", command.get("type"))
                except Exception as exc:
                    logger.error("Control worker error: %s", exc)
                finally:
                    self._control_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Control worker outer error: %s", exc)
                await asyncio.sleep(0.5)
        logger.info("Control worker stopped")

    async def _ad_worker(self) -> None:
        """Dedicated worker for ads — never blocks music play/next."""
        logger.info("Ad worker started")
        while self.running:
            try:
                command = await self._ad_queue.get()
                self._last_command_tick = time.time()
                try:
                    logger.info(
                        "Handling ad command: action=%s campaign=%s",
                        command.get("action"),
                        command.get("campaign_id"),
                    )
                    await asyncio.wait_for(self._handle_command(command), timeout=25.0)
                except asyncio.TimeoutError:
                    logger.error("Ad command timed out: %s", command.get("campaign_id"))
                except Exception as exc:
                    logger.error("Ad worker error: %s", exc)
                finally:
                    self._ad_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Ad worker outer error: %s", exc)
                await asyncio.sleep(0.5)
        logger.info("Ad worker stopped")

    async def _command_worker(self) -> None:
        """Process queued commands sequentially, each with its own timeout, so a
        stuck command (e.g. a stalled ad download) can't block WS message
        processing or other commands indefinitely."""
        logger.info("Command worker started")
        while self.running:
            try:
                command = await self._command_queue.get()
                self._last_command_tick = time.time()
                command_type = command.get("type")
                action = command.get("action")
                logger.info("Handling command: type=%s action=%s", command_type, action)

                timeout_s = 30.0
                if command_type == "ad_control":
                    timeout_s = 25.0
                elif (action or "").upper().replace("-", "_") == "UPDATE_SOFTWARE":
                    timeout_s = 60.0
                elif command_type == "playback_control" and (action or "").lower() in {"play", "next", "skip", "previous"}:
                    # YouTube resolve can take a while on Pi; don't kill it too early,
                    # but never exceed this — subprocess yt-dlp has its own timeout.
                    timeout_s = 70.0

                try:
                    await asyncio.wait_for(self._handle_command(command), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.error(
                        "Command timed out after %.0fs: type=%s action=%s",
                        timeout_s, command_type, action,
                    )
                except Exception as exc:
                    logger.error("Command worker error: %s", exc)
                finally:
                    self._command_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Command worker outer error: %s", exc)
                await asyncio.sleep(0.5)
        logger.info("Command worker stopped")

    async def _watchdog_loop(self) -> None:
        """Detect stalled command queue / stalled playback and recover."""
        while self.running:
            # Polling is only a couple of libvlc getters, so run it often enough that
            # a frozen track is caught within a few seconds rather than half a minute.
            await asyncio.sleep(5.0)
            now = time.time()
            if self._command_queue.qsize() > 0 and self._last_command_tick and now - self._last_command_tick > 40:
                logger.warning(
                    "Watchdog: heavy command queue stuck (size=%s) for %.0fs",
                    self._command_queue.qsize(), now - self._last_command_tick,
                )
            if self._control_queue.qsize() > 0 and self._last_command_tick and now - self._last_command_tick > 20:
                logger.warning(
                    "Watchdog: control queue stuck (size=%s) for %.0fs",
                    self._control_queue.qsize(), now - self._last_command_tick,
                )
            try:
                recovered = await self.player.recover_if_stalled(stall_seconds=8.0)
                if recovered:
                    logger.info("Watchdog recovered stalled playback")
                await self.player.ensure_next_prefetched()
            except Exception as exc:
                logger.warning("Playback stall watchdog failed: %s", exc)

    async def _enqueue_commands(self, commands: list, *, source: str) -> None:
        # Coalesce floods (especially volume after WS reconnect): keep last volume only.
        last_volume: Optional[dict] = None
        filtered: list = []
        for command in commands:
            if command.get("type") == "volume_control":
                last_volume = command
                continue
            filtered.append(command)
        if last_volume is not None:
            filtered.append(last_volume)

        control_n = 0
        heavy_n = 0
        ad_n = 0
        for command in filtered:
            if self._is_ad_command(command):
                await self._ad_queue.put(command)
                ad_n += 1
            elif self._is_priority_control(command):
                await self._control_queue.put(command)
                control_n += 1
            else:
                await self._command_queue.put(command)
                heavy_n += 1
        if control_n or heavy_n or ad_n:
            logger.info(
                "Queued %s control + %s heavy + %s ad command(s) from %s (control_q=%s heavy_q=%s ad_q=%s)",
                control_n,
                heavy_n,
                ad_n,
                source,
                self._control_queue.qsize(),
                self._command_queue.qsize(),
                self._ad_queue.qsize(),
            )

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
                    await self._enqueue_commands(commands, source="ws_pending")
            elif message_type in {"playback_control", "volume_control", "agent_control", "ad_control"}:
                await self._enqueue_commands([data], source="ws_single")
            elif data.get("action"):
                await self._enqueue_commands([data], source="ws_single")

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

        # Adaptive WS cadence:
        # - While playing, send anchors frequently so the dashboard timeline stays accurate.
        # - While idle, still send keepalive status_update every tick so dashboard
        #   last_seen / "alive" does not go stale (HTTP heartbeat is disabled when WS is on).
        playing_interval_s = 5
        idle_interval_s = 20

        while self.running:
            if not (self.ws_client and self.ws_client.connected):
                break

            status = await self.player.get_status()
            pos = status.get("playback_position")
            length = status.get("playback_length")
            is_playing = bool(status.get("is_playing", False))

            payload = {
                "type": "status_update",
                "status": "healthy" if self.player.is_healthy() else "error",
                "current_volume": status.get("volume", 50.0),
                "is_playing": is_playing,
                "current_track_id": status.get("current_track_id"),
                "current_playlist_id": status.get("current_playlist_id"),
                # Keep payload small — full track metadata is not needed every tick.
                "track_position": status.get("track_position"),
                "playback_position": float(pos) if pos is not None else 0.0,
                "playback_length": float(length) if length is not None else 0.0,
                "playback_speed": 1.0,
            }
            # Include current_track only when it changes (less WS/CPU load on Pi).
            if (
                self._last_ws_payload is None
                or self._last_ws_payload.get("current_track_id") != payload.get("current_track_id")
            ):
                payload["current_track"] = status.get("current_track")

            # Always send on every tick (coalesced by WS sender). Cadence:
            # playing every 5s, idle every 20s (keepalive for dashboard last_seen).
            ok = await self.ws_client.send(payload)
            if not ok or not self.ws_client.connected:
                break
            self._last_ws_payload = payload

            self._ws_status_ticks += 1
            if self._ws_status_ticks % 20 == 0:
                # Never block keepalive/status sends on ads config HTTP.
                asyncio.create_task(self._refresh_transition_campaigns())

            await asyncio.sleep(playing_interval_s if is_playing else idle_interval_s)

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
                await self._enqueue_commands(commands, source="long_poll")

            self._ad_config_refresh_counter += 1
            if self._ad_config_refresh_counter % 20 == 0:
                await self._refresh_transition_campaigns()

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
            elif command_type == "ad_control":
                await self._handle_ad_control(command)
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
        elif action == "set_repeat_mode":
            mode = command.get("repeat_mode") or command.get("mode")
            if mode:
                self.player.repeat_mode = str(mode)
                logger.info("Repeat mode set to %s", self.player.repeat_mode)

    async def _handle_volume_control(self, command: dict) -> None:
        volume = command.get("volume")
        if volume is not None:
            await self.player.set_volume(volume)
            # Non-blocking: queue a slim status update (sender coalesces duplicates).
            if self.ws_client and self.ws_client.connected:
                status = await self.player.get_status()
                payload = {
                    "type": "status_update",
                    "status": "healthy" if self.player.is_healthy() else "error",
                    "current_volume": status.get("volume", 50.0),
                    "is_playing": bool(status.get("is_playing", False)),
                    "current_track_id": status.get("current_track_id"),
                    "current_playlist_id": status.get("current_playlist_id"),
                    "track_position": status.get("track_position"),
                    "playback_position": float(status.get("playback_position") or 0.0),
                    "playback_length": float(status.get("playback_length") or 0.0),
                    "playback_speed": 1.0,
                }
                await self.ws_client.send(payload)
                self._last_ws_payload = payload
            logger.info("Volume set to %s", volume)

    async def _handle_ad_control(self, command: dict) -> None:
        if command.get("action") != "play":
            return
        campaign_id = command.get("campaign_id")
        if campaign_id is None:
            logger.warning("ad_control missing campaign_id")
            return
        await self.player.play_ad(
            audio_url=command.get("audio_url") or "",
            campaign_id=int(campaign_id),
            audio_media_id=command.get("audio_media_id"),
            campaign_name=command.get("campaign_name"),
            play_type=command.get("play_type"),
            schedule_config=command.get("schedule_config") or {},
            from_track_end=False,
        )

    async def _on_ad_finished(
        self,
        *,
        campaign_id: int,
        completed: bool,
        duration_played: float,
        error: Optional[str] = None,
    ) -> None:
        payload = {
            "type": "ad_finished",
            "campaign_id": campaign_id,
            "branch_id": self.device.branch_id,
            "completed": completed,
            "duration_played": duration_played,
            "error": error,
        }
        if self.ws_client and self.ws_client.connected:
            await self.ws_client.send(payload)

    async def _refresh_transition_campaigns(self) -> None:
        data = await self.client.get_ad_transition_config()
        if isinstance(data, list):
            self._transition_campaigns = sorted(
                data,
                key=lambda c: (-int(c.get("priority", 0)), int(c.get("id", 0))),
            )
            logger.info("Loaded %s transition ad campaign(s)", len(self._transition_campaigns))

    def _pick_transition_campaign(self) -> Optional[dict]:
        if not self._transition_campaigns:
            return None
        top_priority = self._transition_campaigns[0].get("priority", 0)
        tier = [c for c in self._transition_campaigns if c.get("priority", 0) == top_priority]
        if len(tier) == 1:
            return tier[0]
        strategy = tier[0].get("rotation_strategy", "sequential")
        if strategy == "random":
            return random.choice(tier)
        pick = tier[self._transition_pick_index % len(tier)]
        self._transition_pick_index += 1
        return pick

    async def _on_ad_transition_check(self) -> bool:
        self.player._tracks_since_ad += 1
        campaign = self._pick_transition_campaign()
        if not campaign:
            return False
        every_n = max(1, int(campaign.get("every_n_tracks") or 1))
        if self.player._tracks_since_ad < every_n:
            return False
        self.player._tracks_since_ad = 0
        return await self.player.play_ad(
            audio_url=campaign.get("audio_url") or "",
            campaign_id=int(campaign["id"]),
            audio_media_id=campaign.get("audio_media_id"),
            campaign_name=campaign.get("name"),
            play_type="transition_between_songs",
            schedule_config=campaign.get("schedule_config") or {},
            from_track_end=True,
        )

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
                # Backup last_seen for the dashboard when WS is quiet/wedged.
                await asyncio.sleep(30)
                status = await self.player.get_status()
                response = await self.client.send_heartbeat(self._build_heartbeat(status))
                # The heartbeat endpoint drains the server-side pending queue, so
                # anything it returns has already been removed there. Ignoring the
                # response silently loses commands queued while the WS was down.
                if response:
                    if response.get("repeat_mode"):
                        self.player.repeat_mode = response["repeat_mode"]
                    commands = response.get("commands") or []
                    if commands:
                        await self._enqueue_commands(commands, source="heartbeat")
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
            "track_position": status.get("track_position"),
            "playback_position": status.get("playback_position"),
            "playback_length": status.get("playback_length"),
            "capabilities": self.player.get_capabilities(),
            "version": "1.0.0",
        }
