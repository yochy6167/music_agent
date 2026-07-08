import asyncio
import logging
import platform
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Union

import httpx

logger = logging.getLogger(__name__)

try:
    import vlc  # type: ignore
except Exception:  # pragma: no cover - runtime dependency
    vlc = None


class MusicPlayer:
    CACHE_EXT = ".mp3"

    def __init__(self, api_url: str, device_token: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.device_token = device_token
        self.cache_dir = Path.home() / ".soundops_agent" / "audio_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.instance = None
        self.ad_instance = None
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
        self.on_ad_transition_check: Optional[Callable[[], Union[Awaitable[bool], bool]]] = None
        self.on_ad_finished: Optional[
            Callable[..., Union[Awaitable[None], None]]
        ] = None

        self._ad_lock = asyncio.Lock()
        self._ad_watchdog_task: Optional[asyncio.Task] = None
        self._ad_playing = False
        self._ad_campaign_id: Optional[int] = None
        self._ad_started_at: float = 0.0
        self._ad_resume_was_playing = False
        self._tracks_since_ad = 0
        self.ad_player = None
        self._ad_overlay_mode = "duck"
        self._ad_pre_music_volume: Optional[int] = None
        self._ad_saved_position_ms: Optional[int] = None
        self._ad_after_finish_action: Optional[str] = None
        self._ad_campaign_name: Optional[str] = None
        self._ad_schedule_config: dict = {}

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
            self.ad_instance = self.instance
            if platform.system() == "Windows":
                try:
                    ad_vlc_options = [o for o in vlc_options if not o.startswith("--aout")]
                    ad_vlc_options.append("--aout=waveout")
                    self.ad_instance = vlc.Instance(ad_vlc_options)
                except Exception:
                    self.ad_instance = self.instance
            self.ad_player = self.ad_instance.media_player_new()
            if self.ad_player:
                self.ad_player.audio_set_volume(int(self.volume))
                ad_events = self.ad_player.event_manager()
                ad_events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_ad_end)
            else:
                logger.warning("Failed to create secondary VLC player for ads")
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
        if self.player and not (self._ad_playing and self._ad_overlay_mode == "fade_pause"):
            self.player.audio_set_volume(int(self.volume))
        if self.ad_player and self._ad_playing:
            self._force_ad_player_volume(max(int(self.volume), 80))

    def _ensure_music_audible(self) -> None:
        if not self.player:
            return
        try:
            current = self.player.audio_get_volume()
        except Exception:
            return
        if current is not None and current <= 0:
            target = int(self._ad_pre_music_volume or self.volume or 50)
            self.player.audio_set_volume(max(5, min(100, target)))

    async def play(self, playlist_id: Optional[int] = None, track_id: Optional[int] = None) -> None:
        if not self.player:
            logger.error("VLC player not initialized")
            return
        if self._ad_playing:
            logger.info("Ignoring play() — ad is currently playing")
            return
        self._ensure_music_audible()

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
            "ad_playing": False,
            "ad_campaign_id": None,
            "ad_campaign_name": None,
        }

        state = self.player.get_state()
        is_playing = state == vlc.State.Playing
        position_sec = max(self.player.get_time() / 1000.0, 0.0)
        if self._ad_playing and self._ad_overlay_mode == "fade_pause":
            is_playing = False
            if self._ad_saved_position_ms is not None and self._ad_saved_position_ms >= 0:
                position_sec = self._ad_saved_position_ms / 1000.0
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
            "ad_playing": self._ad_playing,
            "ad_campaign_id": self._ad_campaign_id if self._ad_playing else None,
            "ad_campaign_name": self._ad_campaign_name if self._ad_playing else None,
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

    def _on_ad_end(self, event: Any) -> None:
        if self._event_loop and self._event_loop.is_running():
            asyncio.run_coroutine_threadsafe(self._finish_ad_playback(completed=True), self._event_loop)

    async def _handle_track_end(self) -> None:
        if self._ad_playing:
            if self._ad_overlay_mode == "duck":
                self._ad_after_finish_action = self._ad_after_finish_action or "next"
            return

        if self.on_ad_transition_check:
            try:
                result = self.on_ad_transition_check()
                handled = await result if asyncio.iscoroutine(result) else result
                if handled:
                    return
            except Exception as exc:
                logger.warning("on_ad_transition_check failed: %s", exc)

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

    def _absolutize_url(self, url: str) -> str:
        if not url:
            return url
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("/"):
            return f"{self.api_url}{url}"
        return f"{self.api_url}/{url.lstrip('/')}"

    async def _download_to_cache(
        self, cache_key: int, url: str, dst: Path, timeout: float = 15.0
    ) -> bool:
        try:
            headers = {"X-Device-Token": self.device_token}
            # Short timeout: ad downloads run inline with playback decisions, so we'd
            # rather fail fast and fall back to streaming than freeze for a long time.
            client_timeout = httpx.Timeout(timeout, connect=5.0)
            async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with dst.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            handle.write(chunk)
            return dst.is_file() and dst.stat().st_size > 0
        except Exception as exc:
            logger.error("Ad cache download failed for %s: %s", cache_key, exc)
            return False

    async def _resolve_ad_play_url(self, url: str, cache_key: int) -> Optional[str]:
        abs_url = self._absolutize_url(url)
        cache_path = self.cache_dir / f"ad_{cache_key}{self.CACHE_EXT}"
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return str(cache_path)
        if await self._download_to_cache(cache_key, abs_url, cache_path):
            return str(cache_path)
        return abs_url

    def _force_ad_player_volume(self, volume: int = 100) -> int:
        if not self.ad_player:
            return 0
        vol = max(5, min(100, int(volume)))
        for _ in range(5):
            self.ad_player.audio_set_volume(vol)
            try:
                reported = self.ad_player.audio_get_volume()
                if reported is not None and reported > 0:
                    return reported
            except Exception:
                pass
        return vol

    async def _wait_ad_audio_tail(self, extra_seconds: float = 0.45) -> None:
        ap = self.ad_player
        if not ap:
            return
        try:
            length_ms = ap.get_length()
            for _ in range(60):
                state = ap.get_state()
                pos = ap.get_time()
                if state in (vlc.State.Ended, vlc.State.Stopped):
                    break
                if length_ms > 0 and pos >= 0 and pos >= length_ms - 250:
                    break
                await asyncio.sleep(0.05)
        except Exception:
            pass
        await asyncio.sleep(extra_seconds)

    def _resolve_overlay_mode(
        self,
        play_type: Optional[str],
        schedule_config: Optional[dict],
        music_playing: bool,
        from_track_end: bool,
    ) -> str:
        cfg = schedule_config or {}
        if from_track_end and play_type == "transition_between_songs":
            return "between_tracks"
        if play_type in ("interval_minutes", "scheduled_time"):
            return str(cfg.get("overlay_mode") or "duck")
        return str(cfg.get("overlay_mode") or "duck")

    async def _fade_player_volume(
        self,
        player: Any,
        start_volume: int,
        end_volume: int,
        duration_seconds: float,
    ) -> None:
        if not player:
            return
        if duration_seconds <= 0:
            player.audio_set_volume(max(0, min(100, int(end_volume))))
            return
        steps = max(int(duration_seconds * 20), 1)
        for step in range(1, steps + 1):
            volume = start_volume + (end_volume - start_volume) * step / steps
            player.audio_set_volume(max(0, min(100, int(volume))))
            await asyncio.sleep(duration_seconds / steps)

    async def _apply_music_overlay_before_ad(self, schedule_config: Optional[dict]) -> None:
        cfg = schedule_config or {}
        if not self.player:
            return
        if self._ad_overlay_mode == "between_tracks":
            return
        if self._ad_overlay_mode == "fade_pause":
            fade_out = float(cfg.get("fade_out_seconds") or 2.0)
            current = self.player.audio_get_volume()
            if current < 0:
                current = int(self.volume)
            self._ad_pre_music_volume = current
            try:
                self._ad_saved_position_ms = max(self.player.get_time(), 0)
            except Exception:
                self._ad_saved_position_ms = 0
            await self._fade_player_volume(self.player, current, 0, fade_out)
            return
        duck_percent = int(cfg.get("duck_music_volume_percent") or 25)
        duck_percent = max(5, min(duck_percent, 80))
        current = self.player.audio_get_volume()
        if current < 0:
            current = int(self.volume)
        self._ad_pre_music_volume = current
        await self._fade_player_volume(self.player, current, duck_percent, 0.35)

    async def _restore_music_after_ad(
        self,
        schedule_config: Optional[dict],
        *,
        overlay_mode: str,
        resume_was_playing: bool,
    ) -> None:
        cfg = schedule_config or {}
        if not self.player:
            return
        target = self._ad_pre_music_volume if self._ad_pre_music_volume is not None else int(self.volume)
        if overlay_mode == "between_tracks":
            return
        if not resume_was_playing:
            self.player.audio_set_volume(target)
            return
        if overlay_mode == "fade_pause":
            fade_in = float(cfg.get("fade_in_seconds") or 2.0)
            saved_ms = self._ad_saved_position_ms if self._ad_saved_position_ms is not None else 0
            await self._play_current_track()
            await asyncio.sleep(0.4)
            if saved_ms > 0:
                self.player.set_time(saved_ms)
            self.player.audio_set_volume(0)
            await self._fade_player_volume(self.player, 0, target, fade_in)
            return
        current = self.player.audio_get_volume()
        if current < 0:
            current = target
        await self._fade_player_volume(self.player, current, target, 0.35)

    async def play_ad(
        self,
        audio_url: str,
        campaign_id: int,
        audio_media_id: Optional[int] = None,
        *,
        campaign_name: Optional[str] = None,
        play_type: Optional[str] = None,
        schedule_config: Optional[dict] = None,
        from_track_end: bool = False,
    ) -> bool:
        if not self.player or not self.instance:
            logger.error("Cannot play ad — VLC not initialized")
            return False
        if not self.ad_player:
            logger.error("Ad player not initialized")
            return False

        async with self._ad_lock:
            if self._ad_playing:
                age = time.time() - self._ad_started_at if self._ad_started_at else 999.0
                if age < 120:
                    logger.warning(
                        "Ad already playing (campaign=%s, %.0fs) — ignoring duplicate",
                        self._ad_campaign_id,
                        age,
                    )
                    return False
                await self._finish_ad_playback_unlocked(completed=False, error="stale_replaced")

            status = await self.get_status()
            music_playing = bool(status.get("is_playing"))
            self._ad_resume_was_playing = music_playing
            self._ad_overlay_mode = self._resolve_overlay_mode(
                play_type, schedule_config, music_playing, from_track_end
            )
            self._ad_after_finish_action = "next" if self._ad_overlay_mode == "between_tracks" else None
            self._ad_campaign_name = campaign_name
            self._ad_pre_music_volume = None
            self._ad_saved_position_ms = None

            if music_playing and self._ad_overlay_mode in ("duck", "fade_pause"):
                await self._apply_music_overlay_before_ad(schedule_config)

            if self._ad_overlay_mode == "fade_pause":
                try:
                    self.player.stop()
                except Exception:
                    pass
                await asyncio.sleep(0.25)

            cache_key = audio_media_id if audio_media_id is not None else campaign_id
            logger.info("Starting ad campaign %s mode=%s", campaign_id, self._ad_overlay_mode)
            play_url = await self._resolve_ad_play_url(audio_url, cache_key)
            if not play_url:
                await self._finish_ad_playback_unlocked(
                    completed=False, error="missing_audio_url", schedule_config=schedule_config
                )
                return False

            self._ad_playing = True
            self._ad_campaign_id = campaign_id
            self._ad_started_at = time.time()
            self._ad_schedule_config = schedule_config or {}

            try:
                ad_inst = self.ad_instance or self.instance
                media = ad_inst.media_new(play_url)
                if not media:
                    await self._finish_ad_playback_unlocked(
                        completed=False, error="vlc_media_failed", schedule_config=schedule_config
                    )
                    return False
                self.ad_player.set_media(media)
                result = self.ad_player.play()
                if result != 0:
                    await self._finish_ad_playback_unlocked(
                        completed=False,
                        error=f"vlc_play_code_{result}",
                        schedule_config=schedule_config,
                    )
                    return False
                self._force_ad_player_volume(100)
                await asyncio.sleep(0.15)
                self._force_ad_player_volume(100)
                logger.info("Playing ad campaign %s on overlay player", campaign_id)
                self._start_ad_watchdog(campaign_id)
                return True
            except Exception as exc:
                logger.error("play_ad failed: %s", exc, exc_info=True)
                await self._finish_ad_playback_unlocked(
                    completed=False, error=str(exc), schedule_config=schedule_config
                )
                return False

    def _start_ad_watchdog(self, campaign_id: int) -> None:
        if self._ad_watchdog_task and not self._ad_watchdog_task.done():
            self._ad_watchdog_task.cancel()
        self._ad_watchdog_task = asyncio.create_task(self._watch_ad_playback(campaign_id))

    async def _watch_ad_playback(self, campaign_id: int, max_seconds: float = 300.0) -> None:
        deadline = time.time() + max_seconds
        try:
            while self._ad_playing and time.time() < deadline:
                await asyncio.sleep(0.5)
                ap = self.ad_player
                if not ap:
                    break
                state = ap.get_state()
                if state == vlc.State.Error:
                    async with self._ad_lock:
                        if self._ad_playing and self._ad_campaign_id == campaign_id:
                            await self._finish_ad_playback_unlocked(
                                completed=False, error="vlc_error"
                            )
                    return
                if state in (vlc.State.Ended, vlc.State.Stopped):
                    async with self._ad_lock:
                        if self._ad_playing and self._ad_campaign_id == campaign_id:
                            await self._wait_ad_audio_tail()
                            await self._finish_ad_playback_unlocked(completed=True)
                    return
            if self._ad_playing and self._ad_campaign_id == campaign_id:
                async with self._ad_lock:
                    if self._ad_playing and self._ad_campaign_id == campaign_id:
                        await self._finish_ad_playback_unlocked(
                            completed=False, error="timeout"
                        )
        except asyncio.CancelledError:
            pass

    async def _finish_ad_playback(
        self,
        *,
        completed: bool = True,
        error: Optional[str] = None,
    ) -> None:
        async with self._ad_lock:
            if completed and self._ad_playing and self.ad_player:
                await self._wait_ad_audio_tail()
            await self._finish_ad_playback_unlocked(completed=completed, error=error)

    async def _finish_ad_playback_unlocked(
        self,
        *,
        completed: bool = True,
        error: Optional[str] = None,
        schedule_config: Optional[dict] = None,
    ) -> None:
        if not self._ad_playing and self._ad_campaign_id is None:
            return

        if self._ad_watchdog_task and not self._ad_watchdog_task.done():
            self._ad_watchdog_task.cancel()
            self._ad_watchdog_task = None

        cfg = schedule_config if schedule_config is not None else getattr(self, "_ad_schedule_config", {}) or {}
        overlay_mode = self._ad_overlay_mode
        after_action = self._ad_after_finish_action
        resume_was_playing = self._ad_resume_was_playing
        campaign_id = self._ad_campaign_id
        duration = max(0.0, time.time() - self._ad_started_at) if self._ad_started_at else 0.0

        if self.ad_player:
            try:
                self.ad_player.stop()
            except Exception:
                pass

        self._ad_playing = False
        self._ad_campaign_id = None
        self._ad_campaign_name = None
        self._ad_started_at = 0.0
        self._ad_resume_was_playing = False
        self._ad_overlay_mode = "duck"
        self._ad_after_finish_action = None
        self._ad_schedule_config = {}

        if self.on_ad_finished and campaign_id is not None:
            try:
                result = self.on_ad_finished(
                    campaign_id=campaign_id,
                    completed=completed,
                    duration_played=duration,
                    error=error,
                )
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error("on_ad_finished callback failed: %s", exc, exc_info=True)

        try:
            if overlay_mode == "between_tracks":
                if after_action == "next" and self.current_playlist:
                    await self.next()
            elif overlay_mode in ("duck", "fade_pause"):
                await self._restore_music_after_ad(
                    cfg,
                    overlay_mode=overlay_mode,
                    resume_was_playing=resume_was_playing,
                )
        except Exception as exc:
            logger.warning("Could not restore music after ad: %s", exc)

        self._ad_pre_music_volume = None
        self._ad_saved_position_ms = None

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
                return self._absolutize_url(str(source_url))
            return None

        if source == "youtube":
            youtube_url = source_url or (f"https://www.youtube.com/watch?v={source_id}" if source_id else None)
            if not youtube_url:
                return None
            return self._get_youtube_stream_url(str(youtube_url))

        if source_url:
            return self._absolutize_url(str(source_url))

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
