import asyncio
import inspect
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI

from player import MusicPlayer

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
_AGENT_VERSION: Optional[str] = None
# Updates may only come from this repository. A GitHub account compromise can
# still publish a new tag; pinning git_ref stops a plain push to the default branch.
_ALLOWED_ORIGIN = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:)yochy6167/music_agent(?:\.git)?$",
    re.IGNORECASE,
)
_PINNED_GIT_REF = re.compile(
    r"^(?:[0-9a-f]{40}|v\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.]+)?|refs/tags/[A-Za-z0-9._/-]+)$"
)


def agent_version() -> str:
    """Short git revision the dashboard can use to tell devices apart."""
    global _AGENT_VERSION
    if _AGENT_VERSION:
        return _AGENT_VERSION
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        revision = output.decode().strip()
        if revision:
            _AGENT_VERSION = revision
            return _AGENT_VERSION
    except Exception:
        pass
    _AGENT_VERSION = "2.0.0"
    return _AGENT_VERSION


def _ws_connect_headers(headers: Dict[str, str]) -> Dict[str, Any]:
    """websockets 14+ uses additional_headers; older releases use extra_headers."""
    params = inspect.signature(websockets.connect).parameters
    key = "additional_headers" if "additional_headers" in params else "extra_headers"
    return {key: headers}


# A newer one of these makes queued play/next/previous/seek commands stale.
_INTENT_ACTIONS = {"play", "pause", "stop"}
_SUPERSEDABLE_ACTIONS = {"play", "next", "skip", "previous", "seek"}
# Arriving, one of these makes a track start that is still resolving give way.
_NEW_TRACK_ACTIONS = {"play", "pause", "stop", "next", "skip", "previous"}


def _command_sender(command: dict) -> str:
    """Who sent a command, for the log: a dashboard user or the server itself."""
    user_id = command.get("user_id")
    return f"user {user_id}" if user_id else "system"


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

    async def get_playback_authorization(self) -> Optional[Dict[str, Any]]:
        """Ask the server if a schedule is on. None means stay silent."""
        url = f"{self.api_url}/api/v1/devices/playback-authorization"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("Playback authorization failed: %s", exc)
            return None
        if not isinstance(data, dict) or "should_play" not in data:
            logger.warning("Playback authorization response was not usable")
            return None
        return data

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
                url = f"{self.ws_url}/ws/agent/{self.branch_id}"
                logger.info("Connecting to WebSocket: %s", url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=60,
                    close_timeout=10,
                    max_queue=64,
                    **_ws_connect_headers({"X-Device-Token": self.device_token}),
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
        self._last_transition_refresh: float = 0.0
        self._holiday_ads_paused = False
        self._ws_status_ticks = 0
        self._command_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._control_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._ad_queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._command_worker_task: Optional[asyncio.Task] = None
        self._control_worker_task: Optional[asyncio.Task] = None
        self._ad_worker_task: Optional[asyncio.Task] = None
        self._last_command_tick: float = 0.0
        self._boot_resume_done = False
        self._suppress_resume = False
        self._resume_not_before = 0.0
        # Order of arrival, and the newest play/pause/stop. Commands run on two
        # workers (fast control, slow play/next), so a play queued before a stop
        # used to run after it and leave the branch playing. A heavy command
        # older than the newest play/pause/stop is now skipped as stale.
        self._command_seq = 0
        self._latest_intent_seq = 0
        # The last queued "next" that has not started yet: further next clicks
        # are added to it (skip N) instead of each starting a song.
        self._pending_next: Optional[dict] = None
        self._campaign_refresh_task: Optional[asyncio.Task] = None
        self._ws_established_at = 0.0
        self._status_push_task: Optional[asyncio.Task] = None
        self._status_dirty = False

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.player.set_event_loop(loop)
        self.player.on_track_ended = self._log_track_playback
        self.player.on_ad_transition_check = self._on_ad_transition_check
        self.player.on_ad_finished = self._on_ad_finished
        self.player.on_state_changed = self._on_player_state_changed
        self.player.restore_saved_volume()
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
                        "Handling control command: type=%s action=%s by=%s",
                        command.get("type"),
                        command.get("action"),
                        _command_sender(command),
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
                if command is self._pending_next:
                    self._pending_next = None
                if self._is_superseded(command):
                    logger.info(
                        "Skipping stale command type=%s action=%s (a newer play/pause/stop arrived)",
                        command_type, action,
                    )
                    self._command_queue.task_done()
                    continue
                logger.info(
                    "Handling command: type=%s action=%s by=%s", command_type, action, _command_sender(command)
                )

                timeout_s = 30.0
                if command_type == "ad_control":
                    timeout_s = 25.0
                elif (action or "").upper().replace("-", "_") == "UPDATE_SOFTWARE":
                    # git fetch plus pip and a yt-dlp upgrade on a Pi can take a few minutes.
                    timeout_s = 300.0
                elif command_type == "playback_control" and (action or "").lower() in {
                    "play",
                    "next",
                    "skip",
                    "previous",
                    # A reload waits for any track start already in flight, so it
                    # needs the same headroom as the start it may be queued behind.
                    "reload_playlist",
                }:
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
            self._command_seq += 1
            command["_seq"] = self._command_seq
            action = (command.get("action") or "").lower()
            if command.get("type") == "playback_control" and action in _INTENT_ACTIONS:
                self._latest_intent_seq = self._command_seq
            if command.get("type") == "playback_control" and action in _NEW_TRACK_ACTIONS:
                # A start already resolving for an older command would otherwise
                # play first and be replaced seconds later (two starts per click).
                self.player.supersede_pending_start()
            if self._is_ad_command(command):
                await self._ad_queue.put(command)
                ad_n += 1
            elif self._is_priority_control(command):
                await self._control_queue.put(command)
                control_n += 1
            elif (
                command.get("type") == "playback_control"
                and action in {"next", "skip"}
                and self._pending_next is not None
                and self._pending_next.get("_seq", 0) > self._latest_intent_seq
            ):
                # Another next click while the previous one is still waiting:
                # one skip of N songs instead of N song starts in a row.
                self._pending_next["steps"] = int(self._pending_next.get("steps") or 1) + 1
                self._pending_next["_seq"] = command["_seq"]
            else:
                await self._command_queue.put(command)
                heavy_n += 1
                if command.get("type") == "playback_control" and action in {"next", "skip"}:
                    self._pending_next = command
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

    def _is_superseded(self, command: dict) -> bool:
        """A queued play/next/previous/seek older than the newest play/pause/stop."""
        if command.get("type") != "playback_control":
            return False
        if (command.get("action") or "").lower() not in _SUPERSEDABLE_ACTIONS:
            return False
        seq = command.get("_seq")
        return seq is not None and seq < self._latest_intent_seq

    async def _drain_command_queues(self) -> None:
        """Let a stop that arrived with the handshake finish before we resume."""
        await asyncio.sleep(1.0)
        for queue in (self._control_queue, self._command_queue, self._ad_queue):
            try:
                await asyncio.wait_for(queue.join(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for boot commands before resume")

    async def _resume_if_server_allows(self) -> None:
        """Resume only when the server says a schedule is on.

        No connection, a holiday pause, or no open schedule means silence.
        A local playback file is never enough, including within 6 hours of a
        Friday power cut.
        """
        if self._boot_resume_done or time.monotonic() < self._resume_not_before:
            return
        if self._resume_not_before == 0.0:
            await self._drain_command_queues()
        if self.player._expect_playing:
            self._boot_resume_done = True
            return
        if self._suppress_resume:
            logger.info("Not resuming playback: a stop command arrived")
            self._boot_resume_done = True
            return
        decision = await self.client.get_playback_authorization()
        if decision is None:
            self._resume_not_before = time.monotonic() + 30
            logger.warning("No playback authorization from the server; staying silent")
            return
        self._boot_resume_done = True
        if not decision.get("should_play"):
            logger.info(
                "Server kept playback stopped (%s)",
                decision.get("reason") or "no_active_schedule",
            )
            return
        playlist_id = decision.get("playlist_id")
        if not playlist_id:
            logger.info("Server authorized playback without a playlist; staying silent")
            return
        if decision.get("repeat_mode"):
            self.player.set_repeat_mode(decision.get("repeat_mode"))
        shuffle = decision.get("shuffle")
        if shuffle is not None:
            self.player.set_shuffle(bool(shuffle))
        track_id = self.player.saved_resume_hint(int(playlist_id))
        logger.info(
            "Server authorized schedule resume playlist=%s track=%s",
            playlist_id,
            track_id,
        )
        await self.player.play(
            playlist_id=int(playlist_id),
            track_id=track_id,
            shuffle=bool(shuffle) if shuffle is not None else None,
        )
        if not self.player.current_playlist:
            logger.warning("Authorized resume did not start; will ask the server again")
            self._boot_resume_done = False
            self._resume_not_before = time.monotonic() + 30

    async def _start_websocket_with_retries(self) -> None:
        reconnect_delay = 5.0
        while self.running:
            started = time.monotonic()
            self._ws_established_at = 0.0
            try:
                await self._start_websocket()
                return
            except Exception as exc:
                # After a connection that held, reconnect fast: the delay used to
                # keep growing for the agent's whole life (60s+ per drop).
                if self._ws_established_at and self._ws_established_at >= started:
                    reconnect_delay = 2.0
                logger.warning("WS failed (%s). Retrying in %.0fs", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 60.0)

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
        # A fixed 3s was too short on a slow line: the handshake was cancelled
        # even when it was about to succeed.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.ws_client.connected:
            await asyncio.sleep(0.25)
        if not self.ws_client.connected:
            ws_task.cancel()
            raise RuntimeError("WS connection failed to establish")
        self._ws_established_at = time.monotonic()

        try:
            await self._run_ws_status_loop()
        finally:
            # Stop this client's own reconnect loop too; left running it could
            # finish a handshake later and give the branch a second socket that
            # executes every command twice.
            await self.ws_client.disconnect()
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass
        raise RuntimeError("WS connection lost")

    async def _run_ws_status_loop(self) -> None:
        await self._resume_if_server_allows()

        # Adaptive WS cadence:
        # - While playing, send anchors frequently so the dashboard timeline stays accurate.
        # - While idle, still send keepalive status_update every tick so dashboard
        #   last_seen / "alive" does not go stale (HTTP heartbeat is disabled when WS is on).
        playing_interval_s = 5
        idle_interval_s = 20

        while self.running:
            if not (self.ws_client and self.ws_client.connected):
                break

            await self._resume_if_server_allows()

            status = await self.player.get_status()
            is_playing = bool(status.get("is_playing", False))
            payload = self._build_status_payload(status)

            # Always send on every tick (coalesced by WS sender). Cadence:
            # playing every 5s, idle every 20s (keepalive for dashboard last_seen).
            # Changes in between are pushed at once (_on_player_state_changed).
            ok = await self.ws_client.send(payload)
            if not ok or not self.ws_client.connected:
                break
            self._last_ws_payload = payload

            self._ws_status_ticks += 1
            if self._ws_status_ticks % 20 == 0:
                # Never block keepalive/status sends on ads config HTTP.
                asyncio.create_task(self._refresh_transition_campaigns())

            await asyncio.sleep(playing_interval_s if is_playing else idle_interval_s)

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
                self.player.set_repeat_mode(response["repeat_mode"])
            if "shuffle" in response and response.get("shuffle") is not None:
                self.player.set_shuffle(bool(response["shuffle"]))
            commands = response.get("commands") or []
            if commands:
                await self._enqueue_commands(commands, source="long_poll")
            await self._resume_if_server_allows()

            prev_paused = self._holiday_ads_paused
            if response and "holiday_ads_paused" in response:
                self._holiday_ads_paused = bool(response.get("holiday_ads_paused"))

            self._ad_config_refresh_counter += 1
            if self._ad_config_refresh_counter % 20 == 0 or prev_paused != self._holiday_ads_paused:
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
        # Handle repeat_mode / shuffle independently
        if command.get("repeat_mode"):
            self.player.set_repeat_mode(command.get("repeat_mode"))
            logger.info("Updated repeat_mode to: %s", command.get("repeat_mode"))
        if "shuffle" in command and command.get("shuffle") is not None:
            self.player.set_shuffle(bool(command.get("shuffle")))
            logger.info("Updated shuffle to: %s", command.get("shuffle"))

        if action == "play":
            shuffle = command.get("shuffle")
            await self.player.play(
                playlist_id=command.get("playlist_id"),
                track_id=command.get("track_id"),
                shuffle=shuffle,
                restart_shuffled=bool(shuffle) and not command.get("track_id"),
            )
        elif action == "pause":
            await self.player.pause()
        elif action == "stop":
            # A stop that arrives while booting wins over schedule resume.
            self._suppress_resume = True
            status = await self.player.get_status()
            track_id = status.get("current_track_id")
            # Stop first: the log is an HTTP call (up to 10s), and awaiting it
            # first meant a slow server delayed the stop, held up volume/pause
            # behind it, and could time the stop out before VLC ever stopped.
            await self.player.stop()
            if track_id:
                asyncio.create_task(
                    self._log_track_playback(
                        track_id=int(track_id),
                        playlist_id=status.get("current_playlist_id"),
                        duration_played=float(status.get("playback_position") or 0.0),
                    )
                )
        elif action in {"skip", "next"}:
            await self.player.next(steps=int(command.get("steps") or 1), user=True)
        elif action == "previous":
            await self.player.previous()
        elif action == "seek":
            position = command.get("position")
            if position is None:
                position = command.get("seek_position")
            if position is not None:
                await self.player.seek(position)
                await self._push_status_update()
        elif action == "set_repeat_mode":
            mode = command.get("repeat_mode") or command.get("mode")
            if mode:
                self.player.set_repeat_mode(str(mode))
                logger.info(
                    "Repeat mode set to %s (shuffle=%s)",
                    self.player.repeat_mode,
                    self.player._shuffle_enabled(),
                )
            if "shuffle" in command and command.get("shuffle") is not None:
                self.player.set_shuffle(bool(command.get("shuffle")))
                logger.info("Shuffle set to %s", command.get("shuffle"))
        elif action == "reload_playlist":
            # Sent when a refresh changed the playlist on the server. play() would
            # not re-fetch a playlist it already holds, and would restart the track.
            playlist_id = command.get("playlist_id")
            if playlist_id is None or str(playlist_id) == str(self.player.current_playlist_id):
                await self.player.reload_playlist(playlist_id)
                await self._push_status_update()
            else:
                logger.info(
                    "Ignoring reload for playlist %s — this device has %s loaded",
                    playlist_id,
                    self.player.current_playlist_id,
                )

    def _build_status_payload(self, status: dict) -> dict:
        return {
            "type": "status_update",
            "status": "healthy" if self.player.is_healthy() else "error",
            "current_volume": status.get("volume", 50.0),
            "is_playing": bool(status.get("is_playing", False)),
            # loading / playing / paused / ad / stopped — what the dashboard shows
            "state": status.get("state"),
            "loading_track": status.get("loading_track"),
            "current_track_id": status.get("current_track_id"),
            "current_playlist_id": status.get("current_playlist_id"),
            # Every time, not only on a change: the sender keeps just the newest
            # status, so a title sent once could be replaced by a tick without it.
            "current_track": status.get("current_track"),
            "track_position": status.get("track_position"),
            "playback_position": float(status.get("playback_position") or 0.0),
            "playback_length": float(status.get("playback_length") or 0.0),
            "playback_speed": 1.0,
            "ad_playing": bool(status.get("ad_playing")),
            "ad_campaign_name": status.get("ad_campaign_name"),
        }

    def _on_player_state_changed(self) -> None:
        """Push the new state now (a burst of changes becomes one update)."""
        self._status_dirty = True
        if self._status_push_task and not self._status_push_task.done():
            return
        try:
            self._status_push_task = asyncio.get_running_loop().create_task(self._push_status_soon())
        except RuntimeError:
            pass

    async def _push_status_soon(self) -> None:
        # A change while one push is under way gets a push of its own.
        while self._status_dirty:
            self._status_dirty = False
            # pause -> stop or start -> playing often land in the same moment
            await asyncio.sleep(0.05)
            try:
                await self._push_status_update()
            except Exception as exc:
                logger.debug("Status push failed: %s", exc)

    async def _push_status_update(self) -> None:
        """Send an anchor immediately instead of waiting for the next status tick.

        Used after commands that move the timeline; otherwise the dashboard keeps
        extrapolating from the pre-command anchor for up to a full interval.
        """
        if not (self.ws_client and self.ws_client.connected):
            return
        payload = self._build_status_payload(await self.player.get_status())
        await self.ws_client.send(payload)
        self._last_ws_payload = payload

    async def _handle_volume_control(self, command: dict) -> None:
        volume = command.get("volume")
        if volume is not None:
            await self.player.set_volume(volume)
            # Non-blocking: queue a slim status update (sender coalesces duplicates).
            await self._push_status_update()
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
            self._last_transition_refresh = time.time()
            logger.info("Loaded %s transition ad campaign(s)", len(self._transition_campaigns))

    async def _safe_refresh_transition_campaigns(self) -> None:
        try:
            await self._refresh_transition_campaigns()
        except Exception as exc:
            logger.warning("Failed to refresh transition campaigns: %s", exc)
        finally:
            # A failed refresh waits a minute too, instead of retrying every song.
            self._last_transition_refresh = max(self._last_transition_refresh, time.time() - 30)

    def _pick_transition_campaign(self) -> Optional[dict]:
        if self._holiday_ads_paused or not self._transition_campaigns:
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
        # Refresh in the background and decide with what is cached: this runs at
        # the end of every song, and waiting on the server here (up to 10s) was
        # silence between songs.
        if time.time() - self._last_transition_refresh >= 60 and not (
            self._campaign_refresh_task and not self._campaign_refresh_task.done()
        ):
            self._campaign_refresh_task = asyncio.create_task(self._safe_refresh_transition_campaigns())
        if self._holiday_ads_paused:
            return False
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

    def _pinned_git_ref(self, command: dict) -> str:
        raw = command.get("git_ref") or os.environ.get("MUSIC_AGENT_GIT_REF") or ""
        return str(raw).strip()

    async def _run_exec(self, argv: List[str]) -> tuple:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(_REPO_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        out_text = stdout.decode(errors="replace")
        err_text = stderr.decode(errors="replace")
        for line in out_text.splitlines():
            if line.strip():
                logger.info(line.rstrip())
        for line in err_text.splitlines():
            if line.strip():
                logger.error(line.rstrip())
        return process.returncode or 0, out_text, err_text

    async def _handle_update_software(self, command: dict) -> None:
        logger.info("Starting remote update via Git...")
        await self._send_update_notice("Starting remote update via Git...")

        code, origin, _err = await self._run_exec(["git", "remote", "get-url", "origin"])
        origin = origin.strip()
        if code != 0 or not _ALLOWED_ORIGIN.fullmatch(origin):
            logger.error("Refusing update: origin %r is not the music_agent repository", origin)
            await self._send_update_notice("Remote update refused: unexpected git origin.")
            await self._send_ws_log("Git origin is not the expected repository. Update aborted.")
            return

        git_ref = self._pinned_git_ref(command)
        if git_ref:
            if not _PINNED_GIT_REF.fullmatch(git_ref) or ".." in git_ref:
                logger.error("Refusing update: invalid git_ref %r", git_ref)
                await self._send_update_notice("Remote update refused: invalid git ref.")
                await self._send_ws_log("Invalid git ref. Update aborted.")
                return
            await self._send_ws_log(f"Fetching pinned ref {git_ref}...")
            if re.fullmatch(r"[0-9a-f]{40}", git_ref):
                code, _out, err = await self._run_exec(["git", "fetch", "origin"])
                if code != 0:
                    await self._send_update_notice("Remote update failed during git fetch.")
                    await self._send_ws_log("Git fetch failed. Update aborted.")
                    return
                code, _out, err = await self._run_exec(
                    ["git", "rev-parse", "--verify", f"{git_ref}^{{commit}}"]
                )
                if code != 0:
                    await self._send_update_notice("Pinned commit is not in the fetched history.")
                    await self._send_ws_log("Pinned commit was not found. Update aborted.")
                    return
                code, _out, err = await self._run_exec(["git", "checkout", "--detach", git_ref])
            else:
                tag_ref = git_ref if git_ref.startswith("refs/tags/") else f"refs/tags/{git_ref}"
                code, _out, err = await self._run_exec(["git", "fetch", "origin", tag_ref])
                if code != 0:
                    await self._send_update_notice("Remote update failed during git fetch.")
                    await self._send_ws_log("Git fetch failed. Update aborted.")
                    return
                code, _out, err = await self._run_exec(["git", "checkout", "--detach", "FETCH_HEAD"])
        else:
            logger.warning(
                "Update has no pinned git_ref; fast-forwarding the current branch. "
                "Set MUSIC_AGENT_GIT_REF or send git_ref to pin a tag or commit."
            )
            await self._send_ws_log("Pulling latest code (fast-forward only)...")
            code, _out, err = await self._run_exec(["git", "pull", "--ff-only"])

        if code != 0:
            logger.error("Git update failed (%s): %s", code, (err or "").strip())
            await self._send_update_notice("Remote update failed during git update.")
            await self._send_ws_log("Git update failed. Update aborted.")
            return

        await self._send_ws_log("Installing Python dependencies...")
        venv_pip = _REPO_ROOT / ".venv" / "bin" / "pip"
        pip = str(venv_pip) if venv_pip.is_file() else "pip"
        requirements = _REPO_ROOT / "requirements.txt"
        if requirements.is_file():
            code, _out, err = await self._run_exec([pip, "install", "-r", str(requirements)])
            if code != 0:
                logger.error("pip install failed (%s): %s", code, (err or "").strip())
                await self._send_update_notice("Remote update failed during pip install.")
                await self._send_ws_log("pip install failed. Update aborted.")
                return
        code, _out, err = await self._run_exec([pip, "install", "-U", "yt-dlp[default]"])
        if code != 0:
            logger.error("yt-dlp upgrade failed (%s): %s", code, (err or "").strip())
            await self._send_update_notice("Remote update failed while upgrading yt-dlp.")
            await self._send_ws_log("yt-dlp upgrade failed. Update aborted.")
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
                    prev_paused = self._holiday_ads_paused
                    if "holiday_ads_paused" in response:
                        self._holiday_ads_paused = bool(response.get("holiday_ads_paused"))
                    if prev_paused != self._holiday_ads_paused:
                        await self._refresh_transition_campaigns()
                    if response.get("repeat_mode"):
                        self.player.set_repeat_mode(response["repeat_mode"])
                    if "shuffle" in response and response.get("shuffle") is not None:
                        self.player.set_shuffle(bool(response["shuffle"]))
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
            # The same picture as the WS status: when WS is down this is all the
            # dashboard gets, and a new track id without its title left the old one.
            "current_track": status.get("current_track"),
            "state": status.get("state"),
            "loading_track": status.get("loading_track"),
            "ad_playing": bool(status.get("ad_playing")),
            "ad_campaign_name": status.get("ad_campaign_name"),
            "track_position": status.get("track_position"),
            "playback_position": status.get("playback_position"),
            "playback_length": status.get("playback_length"),
            "capabilities": self.player.get_capabilities(),
            "version": agent_version(),
        }
