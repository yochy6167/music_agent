import asyncio
import logging
import platform
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Union

import httpx

logger = logging.getLogger(__name__)

try:
    import vlc  # type: ignore
except Exception:  # pragma: no cover - runtime dependency
    vlc = None


class MusicPlayer:
    def __init__(self, api_url: str, device_token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.device_token = device_token
        self.instance = None
        self.player = None
        self.current_playlist_id: Optional[int] = None
        self.current_track_id: Optional[int] = None
        self.current_playlist: list = []
        self.current_index: int = 0
        self.volume: float = 50.0
        self.repeat_mode: str = "repeat_all"
        self.current_track: Optional[Dict[str, Any]] = None
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self.on_track_ended: Optional[
            Callable[[int, Optional[int], float], Union[Awaitable[None], None]]
        ] = None

        self._init_vlc()

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._event_loop = loop

    def _init_vlc(self) -> None:
        if vlc is None:
            logger.error("python-vlc not installed or VLC missing")
            return

        vlc_options = [
            "--intf",
            "dummy",
            "--no-video",
            "--no-xlib",
            "--quiet",
        ]
        vlc_options.extend(
            [
                "--file-caching=3000",
                "--network-caching=5000",
                "--clock-jitter=500",
            ]
        )
        if platform.system() == "Windows":
            vlc_options.append("--aout=adp")
        elif platform.system() == "Linux":
            vlc_options.append("--aout=pulse")
        try:
            self.instance = vlc.Instance(vlc_options)
            self.player = self.instance.media_player_new()
            self.player.audio_set_volume(int(self.volume))
            event_manager = self.player.event_manager()
            event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_track_end)
            logger.info("VLC initialized (headless)")
        except Exception as exc:
            logger.error("VLC init failed: %s", exc)
            self.instance = None
            self.player = None

    def is_healthy(self) -> bool:
        return self.player is not None

    def get_capabilities(self) -> Dict[str, bool]:
        return {
            "spotify": False,
            "youtube": True,
            "local": True,
            "stream": True,
        }

    async def set_volume(self, volume: float) -> None:
        self.volume = float(volume)
        if self.player:
            self.player.audio_set_volume(int(self.volume))

    async def play(self, playlist_id: Optional[int] = None, track_id: Optional[int] = None) -> None:
        if not self.player:
            logger.error("VLC player not initialized")
            return

        if playlist_id and (playlist_id != self.current_playlist_id or not self.current_playlist):
            await self._load_playlist(int(playlist_id))

        if not self.current_playlist:
            logger.error("No playlist loaded")
            return

        if track_id is not None:
            self._set_index_for_track(track_id)
        else:
            if self.current_index >= len(self.current_playlist):
                self.current_index = 0

        await self._play_current_track()

    async def pause(self) -> None:
        if self.player:
            self.player.pause()

    async def stop(self) -> None:
        if self.player:
            self.player.stop()

    async def next(self) -> None:
        if not self.current_playlist:
            return
        self.current_index = (self.current_index + 1) % len(self.current_playlist)
        await self._play_current_track()

    async def previous(self) -> None:
        if not self.current_playlist:
            return
        self.current_index = (self.current_index - 1) % len(self.current_playlist)
        await self._play_current_track()

    async def seek(self, position_seconds: float) -> None:
        if self.player:
            self.player.set_time(int(float(position_seconds) * 1000))

    async def get_status(self) -> Dict[str, Any]:
        if not self.player:
            return {
                "is_playing": False,
                "volume": self.volume,
                "current_track_id": self.current_track_id,
                "current_playlist_id": self.current_playlist_id,
                "current_track": self.current_track,
                "track_position": None,
                "playback_position": 0.0,
                "playback_length": 0.0,
            }

        state = self.player.get_state()
        is_playing = state == vlc.State.Playing
        position_sec = max(self.player.get_time() / 1000.0, 0.0)
        length_sec = max(self.player.get_length() / 1000.0, 0.0)
        track_position = None
        if self.current_playlist:
            track_position = {"index": self.current_index, "total": len(self.current_playlist)}
        return {
            "is_playing": is_playing,
            "volume": self.volume,
            "current_track_id": self.current_track_id,
            "current_playlist_id": self.current_playlist_id,
            "current_track": self.current_track,
            "track_position": track_position,
            "playback_position": position_sec,
            "playback_length": length_sec,
        }

    async def _load_playlist(self, playlist_id: int) -> None:
        playlist_data = await self._fetch_playlist(playlist_id)
        if not playlist_data:
            logger.error("Failed to load playlist %s", playlist_id)
            self.current_playlist = []
            return

        items = playlist_data.get("items", [])
        self.current_playlist = items
        self.current_playlist_id = playlist_id
        self.current_index = 0
        logger.info("Loaded playlist %s (%d items)", playlist_id, len(items))

    async def _fetch_playlist(self, playlist_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.api_url}/api/v1/playlists/{playlist_id}"
        headers = {"X-Device-Token": self.device_token}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("Playlist fetch failed: %s", exc)
            return None

    def _set_index_for_track(self, track_id: Any) -> None:
        for idx, track in enumerate(self.current_playlist):
            item_id = track.get("id")
            if str(item_id) == str(track_id):
                self.current_index = idx
                return
        logger.warning("Track %s not found in playlist", track_id)

    async def _play_current_track(self) -> None:
        if not self.current_playlist or not self.player:
            return
        track = self.current_playlist[self.current_index]
        media_url = self._get_media_url(track)
        if not media_url:
            logger.warning("No playable URL, skipping track %s", track.get("id"))
            await self.next()
            return

        media = self.instance.media_new(media_url)
        self.player.set_media(media)
        self.player.play()
        await asyncio.sleep(0.3)
        self.current_track_id = track.get("id")
        self.current_track = track
        logger.info("Playing track %s", self.current_track_id)

    def _on_track_end(self, event: Any) -> None:
        if self._event_loop and self._event_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._handle_track_end(), self._event_loop)

    async def _handle_track_end(self) -> None:
        ended_track_id = self.current_track_id
        ended_playlist_id = self.current_playlist_id
        duration_played = 0.0
        if self.player:
            try:
                duration_played = max(self.player.get_time() / 1000.0, 0.0)
            except Exception:
                duration_played = 0.0

        if self.on_track_ended and ended_track_id is not None:
            try:
                result = self.on_track_ended(
                    int(ended_track_id),
                    int(ended_playlist_id) if ended_playlist_id is not None else None,
                    duration_played,
                )
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("on_track_ended callback failed: %s", exc)

        if self.repeat_mode == "repeat_one":
            await self._play_current_track()
            return

        if not self.current_playlist:
            return

        if self.repeat_mode == "play_once":
            if self.current_index >= len(self.current_playlist) - 1:
                await self.stop()
                return

        await self.next()

    def _get_media_url(self, track: Dict[str, Any]) -> Optional[str]:
        source = track.get("source")
        source_url = track.get("source_url")
        source_id = track.get("source_id")

        if source == "local":
            file_path = track.get("file_path")
            if file_path:
                path = Path(file_path)
                if path.exists():
                    return str(path.resolve())
            if source_url and not str(source_url).startswith("blob:"):
                return source_url
            return None

        if source == "youtube":
            youtube_url = source_url or (f"https://www.youtube.com/watch?v={source_id}" if source_id else None)
            if not youtube_url:
                return None
            return self._get_youtube_stream_url(str(youtube_url))

        if source_url:
            return str(source_url)

        return None

    def _get_youtube_stream_url(self, youtube_url: str) -> Optional[str]:
        try:
            import yt_dlp

            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "skip_download": True,
                "format": "bestaudio/best",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
                if "url" in info:
                    return info["url"]
                if "formats" in info and info["formats"]:
                    return info["formats"][-1].get("url")
            return None
        except Exception as exc:
            logger.error("yt-dlp error: %s", exc)
            return None
