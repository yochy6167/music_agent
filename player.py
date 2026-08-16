import asyncio
import logging
import os
import platform
import random
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
    # A track whose position has not moved for this long is treated as dead and restarted.
    FROZEN_TIMEOUT_S = 12.0
    # Time after a start during which a non-advancing position is still normal (buffer fill).
    FROZEN_GRACE_S = 15.0
    # Restarts of the same track before giving up on it and advancing.
    FROZEN_MAX_RESTARTS = 2
    RESOLVE_BACKOFF_BASE_S = 120.0
    RESOLVE_BACKOFF_MAX_S = 3600.0
    # Consecutive unresolvable tracks that suggest the playlist itself is stale.
    RELOAD_AFTER_FAILURES = 3
    # Floor between self-triggered reloads, so a bad network cannot cause a storm.
    RELOAD_COOLDOWN_S = 300.0
    # Resolve the next track once the current one is this far through.
    PREFETCH_AT_FRACTION = 0.60
    # Cap for the above, so tracks of unknown length still get prefetched.
    PREFETCH_MAX_WAIT_S = 150.0
    # Silence held after a seek while VLC refetches and refills its buffer.
    SEEK_REBUFFER_S = 1.2
    # Extra silence after un-pausing, covering the audio output restart.
    SEEK_RESUME_MUTE_S = 0.3

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
        self.shuffle: bool = False
        self._original_playlist: list = []
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
        self._expect_playing = False
        self._stall_since: Optional[float] = None
        # track_id -> (media_url, expires_at_monotonic)
        self._url_cache: Dict[str, tuple] = {}
        # track_id -> (failure_count, retry_not_before_monotonic)
        self._failed_urls: Dict[str, tuple] = {}
        self._prefetch_task: Optional[asyncio.Task] = None
        # Kept well under YouTube's link lifetime; a stale entry means a failed start.
        self._url_cache_ttl_s = 15 * 60
        self._play_started_at: float = 0.0
        self._last_known_position_s: float = 0.0
        self._play_lock = asyncio.Lock()
        self._progress_position_ms: int = -1
        self._progress_changed_at: float = time.monotonic()
        self._seeked_at: float = 0.0
        self._frozen_track_id: Optional[Any] = None
        self._frozen_restarts: int = 0
        self._last_reload_at: float = 0.0
        self._resolve_failures_since_reload: int = 0

        self._init_vlc()

    def _shuffle_enabled(self) -> bool:
        return bool(self.shuffle) or self.repeat_mode == "shuffle"

    def set_repeat_mode(self, mode: Optional[str]) -> None:
        """Update repeat/shuffle mode and re-order the loaded playlist if needed."""
        if not mode:
            return
        previous = self._shuffle_enabled()
        current_id = self.current_track_id
        self.repeat_mode = str(mode)
        self.shuffle = self.repeat_mode == "shuffle"
        now_shuffle = self._shuffle_enabled()
        if now_shuffle and not previous:
            self._reshuffle_playlist(keep_track_id=current_id)
            if current_id is not None:
                self.current_index = 0
            logger.info("Shuffle enabled — playlist order randomized")
        elif previous and not now_shuffle:
            self._restore_original_order(keep_track_id=current_id)
            logger.info("Shuffle disabled — restored playlist order")

    def _reshuffle_playlist(self, keep_track_id=None, avoid_first_id=None) -> None:
        """Fisher-Yates shuffle of the canonical playlist. No song repeats in a cycle."""
        source = list(self._original_playlist or self.current_playlist or [])
        if len(source) <= 1:
            self.current_playlist = source
            return
        shuffled = source[:]
        random.shuffle(shuffled)
        if (
            avoid_first_id is not None
            and shuffled
            and str(shuffled[0].get("id")) == str(avoid_first_id)
            and len(shuffled) > 1
        ):
            swap_with = random.randint(1, len(shuffled) - 1)
            shuffled[0], shuffled[swap_with] = shuffled[swap_with], shuffled[0]
        if keep_track_id is not None:
            for i, track in enumerate(shuffled):
                if str(track.get("id")) == str(keep_track_id):
                    shuffled.insert(0, shuffled.pop(i))
                    break
        self.current_playlist = shuffled
        logger.info(
            "Shuffled playlist (%s tracks), first=%s",
            len(shuffled),
            shuffled[0].get("title") if shuffled else None,
        )

    def _restore_original_order(self, keep_track_id=None) -> None:
        if self._original_playlist:
            self.current_playlist = list(self._original_playlist)
        if keep_track_id is not None and self.current_playlist:
            for i, track in enumerate(self.current_playlist):
                if str(track.get("id")) == str(keep_track_id):
                    self.current_index = i
                    return
        if not self.current_playlist:
            self.current_index = 0
            return
        if self.current_index is None or self.current_index >= len(self.current_playlist):
            self.current_index = 0

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
        # network-caching is the PTS delay: how far ahead VLC schedules the first
        # sample. The previous 60000 meant a 60s delay before the track was considered
        # started, which delays recovery and inflates memory for no benefit — 5s is
        # ample for background music. clock-jitter/clock-synchro are deliberately left
        # at their defaults; pinning them to 0 removes all drift tolerance, so ordinary
        # HTTP arrival jitter gets treated as a stream discontinuity.
        vlc_options.extend(
            [
                "--file-caching=3000",
                "--network-caching=5000",
                "--http-reconnect",
            ]
        )
        if platform.system() == "Windows":
            vlc_options.append("--aout=adp")
        elif platform.system() == "Linux":
            # VLC's native PulseAudio output glitches badly on the Pi: measured over
            # 45s of the same track it produced repeated "cannot synchronize start",
            # buffer underruns and dropped buffers, heard as audio cutting in and out.
            # Its ALSA output was clean in the same test. ALSA "default" still reaches
            # PulseAudio through the alsa-pulse plugin, so pactl volume control, the
            # ad/music mixing and HDMI-vs-jack selection all keep working — only the
            # faulty output module is bypassed.
            vlc_options.append("--aout=alsa")
            alsa_device = os.environ.get("ALSA_AUDIO_DEVICE", "default").strip()
            if alsa_device:
                vlc_options.append(f"--alsa-audio-device={alsa_device}")
        try:
            self.instance = vlc.Instance(vlc_options)
            self.player = self.instance.media_player_new()
            self.player.audio_set_volume(int(self.volume))
            event_manager = self.player.event_manager()
            event_manager.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_track_end)
            self.ad_instance = self.instance
            ad_aout = {"Windows": "waveout", "Linux": "pulse"}.get(platform.system())
            if ad_aout:
                # The ad needs a different output module than the music, not just a
                # separate player. Two ALSA clients starve this card as soon as they
                # overlap: measured over six ads, sharing ALSA gave 12.6s of dead
                # air that got worse with each ad, while sending only the ad through
                # PulseAudio gave 2.9s and improved with each ad. The music itself
                # must stay on ALSA — VLC's PulseAudio output cannot hold a long
                # HTTP stream here (28s of dropouts in a 25s window).
                try:
                    ad_vlc_options = [
                        o for o in vlc_options
                        if not o.startswith(("--aout", "--alsa-audio-device"))
                    ]
                    ad_vlc_options.append(f"--aout={ad_aout}")
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

    def _intended_music_volume(self) -> int:
        """Volume the music should return to once an ad finishes.

        audio_get_volume() alone is not safe here: a track that is still warming
        up is deliberately muted, so an ad starting in that window would capture
        0 and leave the music silent for good after the ad.
        """
        try:
            current = self.player.audio_get_volume() if self.player else -1
        except Exception:
            current = -1
        if current is not None and current > 0:
            return int(current)
        return max(int(self.volume or 50), 5)

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

    async def play(
        self,
        playlist_id: Optional[int] = None,
        track_id: Optional[int] = None,
        shuffle: Optional[bool] = None,
        restart_shuffled: bool = False,
    ) -> None:
        if not self.player:
            logger.error("VLC player not initialized")
            return
        if self._ad_playing:
            logger.info("Ignoring play() — ad is currently playing")
            return
        self._ensure_music_audible()

        if shuffle is True:
            self.set_repeat_mode("shuffle")
        elif shuffle is False and self.repeat_mode == "shuffle":
            self.set_repeat_mode("repeat_all")
        if shuffle is True and track_id is None:
            restart_shuffled = True

        if playlist_id and (playlist_id != self.current_playlist_id or not self.current_playlist):
            await self._load_playlist(int(playlist_id))

        if not self.current_playlist:
            logger.error("No playlist loaded")
            return

        if self._shuffle_enabled() and self.current_playlist:
            if restart_shuffled and track_id is None:
                self._reshuffle_playlist()
                self.current_index = 0
                logger.info("Starting shuffled playlist from the beginning")
            elif track_id is not None:
                self._reshuffle_playlist(keep_track_id=track_id)
                self.current_index = 0

        if track_id is not None:
            self._set_index_for_track(track_id)
        else:
            if self.current_index >= len(self.current_playlist):
                self.current_index = 0

        target = self.current_playlist[self.current_index]
        target_id = target.get("id")
        try:
            state = self.player.get_state()
        except Exception:
            state = None

        same_track = (
            target_id is not None
            and self.current_track_id is not None
            and str(target_id) == str(self.current_track_id)
        )
        active_states = set()
        if vlc:
            active_states = {
                vlc.State.Playing,
                vlc.State.Buffering,
                vlc.State.Opening,
            }

        # CRITICAL: dashboard often re-sends play while VLC is still Buffering/Opening
        # after a YouTube start. Re-calling play() restarts the track from 0 in a loop.
        if same_track and state in active_states:
            logger.info(
                "Already active on track %s (state=%s) — ignoring duplicate play",
                target_id,
                getattr(state, "name", state),
            )
            self._expect_playing = True
            return

        if same_track and vlc and state == vlc.State.Paused:
            logger.info("Resuming paused track %s", target_id)
            self.player.set_pause(False)
            self._expect_playing = True
            self._stall_since = None
            self._progress_position_ms = -1
            self._progress_changed_at = time.monotonic()
            self._play_started_at = time.monotonic()
            return

        # Grace window: even if state flickers to Stopped for a moment, don't restart.
        if (
            same_track
            and self._expect_playing
            and self._play_started_at
            and (time.monotonic() - self._play_started_at) < 20.0
            and state not in ({vlc.State.Ended, vlc.State.Error} if vlc else set())
        ):
            logger.info(
                "Track %s started %.1fs ago — ignoring duplicate play (state=%s)",
                target_id,
                time.monotonic() - self._play_started_at,
                getattr(state, "name", state),
            )
            return

        self._expect_playing = True
        await self._play_current_track()

    async def pause(self) -> None:
        if not self.player:
            return
        try:
            state = self.player.get_state()
        except Exception:
            state = None
        # libvlc pause() TOGGLES — must use set_pause(True) or duplicate
        # pause commands from the dashboard resume playback unexpectedly.
        if vlc and state == vlc.State.Paused:
            logger.info("Already paused — ignoring duplicate pause")
            self._expect_playing = False
            self._stall_since = None
            return
        if vlc and state in (vlc.State.Stopped, vlc.State.Ended, vlc.State.Error):
            self._expect_playing = False
            self._stall_since = None
            return
        self.player.set_pause(True)
        self._expect_playing = False
        self._stall_since = None
        logger.info("Paused playback")

    async def stop(self) -> None:
        if self.player:
            self.player.stop()
            self._expect_playing = False
            self._stall_since = None

    async def next(self) -> None:
        if not self.current_playlist:
            return
        at_end = self.current_index >= len(self.current_playlist) - 1
        if at_end and self._shuffle_enabled():
            last_id = self.current_playlist[self.current_index].get("id")
            self._reshuffle_playlist(avoid_first_id=last_id)
            self.current_index = 0
            logger.info("Reshuffled playlist for a new cycle")
        else:
            self.current_index = (self.current_index + 1) % len(self.current_playlist)
        self._expect_playing = True
        await self._play_current_track()

    async def previous(self) -> None:
        if not self.current_playlist:
            return
        self.current_index = (self.current_index - 1) % len(self.current_playlist)
        self._expect_playing = True
        await self._play_current_track()

    async def seek(self, position_seconds: float) -> None:
        if not self.player:
            return
        target_ms = max(int(float(position_seconds) * 1000), 0)
        try:
            length_ms = self.player.get_length()
        except Exception:
            length_ms = 0
        if length_ms and length_ms > 0:
            target_ms = min(target_ms, max(length_ms - 1000, 0))

        # A seek makes VLC re-request the HTTP range, but it keeps feeding the
        # audio output while the buffer is starved. Measured at the speaker, a
        # backward seek produced ~1.7s of ~150ms gaps — audible as machine-gun
        # stuttering. Pausing across the refill lets the buffer fill in silence
        # and turns that into one short, clean gap.
        resume_volume: Optional[int] = None
        bridged = self._expect_playing and not self._ad_playing
        if bridged:
            try:
                current = self.player.audio_get_volume()
                if current is not None and current > 0:
                    resume_volume = int(current)
                    self.player.audio_set_volume(0)
                self.player.set_pause(True)
            except Exception:
                bridged = False

        self.player.set_time(target_ms)

        # Set before the wait so a status read during it reports the target, not
        # the pre-seek position: the clamp in get_status() would otherwise treat
        # the post-seek 0 as a buffering artefact, and the stall watchdog would
        # read the jump as "position stopped advancing".
        self._seeked_at = time.monotonic()
        self._last_known_position_s = target_ms / 1000.0
        self._progress_position_ms = -1
        self._progress_changed_at = time.monotonic()
        self._stall_since = None
        logger.info("Seek to %.1fs", target_ms / 1000.0)

        if bridged:
            await asyncio.sleep(self.SEEK_REBUFFER_S)
            try:
                self.player.set_pause(False)
                # Restarting the audio output costs ~150ms of silence. Staying
                # muted across it keeps that inside the one intentional gap
                # instead of surfacing as a second glitch after audio resumed.
                await asyncio.sleep(self.SEEK_RESUME_MUTE_S)
                if resume_volume is not None:
                    self.player.audio_set_volume(resume_volume)
            except Exception:
                pass
            self._seeked_at = time.monotonic()
            self._progress_changed_at = time.monotonic()

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
        # Treat Buffering/Opening as playing so the dashboard doesn't flicker "stopped"
        # during network hiccups while audio is still coming out of the buffer.
        if vlc and state in (vlc.State.Playing, vlc.State.Buffering, vlc.State.Opening):
            is_playing = True
        elif vlc and state in (vlc.State.Ended, vlc.State.Error):
            is_playing = False
        elif vlc and state == vlc.State.Stopped:
            is_playing = False
        else:
            is_playing = bool(self._expect_playing)
        position_sec = max(self.player.get_time() / 1000.0, 0.0)
        if self._ad_playing and self._ad_overlay_mode == "fade_pause":
            is_playing = False
            if self._ad_saved_position_ms is not None and self._ad_saved_position_ms >= 0:
                position_sec = self._ad_saved_position_ms / 1000.0
        # YouTube/VLC often reports time=0 while Buffering; don't tell the dashboard
        # we jumped back to the start (that triggers another play command).
        if is_playing and position_sec <= 0.25 and self._last_known_position_s > 1.0:
            position_sec = self._last_known_position_s
        elif position_sec > 0.25:
            self._last_known_position_s = position_sec
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
            self._original_playlist = []
            return

        items = playlist_data.get("items", [])
        self._original_playlist = list(items)
        self.current_playlist_id = playlist_id
        if self._shuffle_enabled() and items:
            self._reshuffle_playlist()
            self.current_index = 0
        else:
            self.current_playlist = items
            self.current_index = 0
        logger.info("Loaded playlist %s (%d items)", playlist_id, len(items))

    async def reload_playlist(self, playlist_id: Optional[int] = None) -> bool:
        """Re-fetch the loaded playlist without interrupting what is playing.

        A refresh on the server can delete tracks, and the agent otherwise keeps
        serving the list it fetched when playback started: every deleted track is
        then a failed resolve and several seconds of silence. play() cannot be
        reused for this because it deliberately skips loading when the playlist is
        already loaded, and it would restart the track from zero.
        """
        target = int(playlist_id or self.current_playlist_id or 0)
        if not target:
            logger.warning("Playlist reload requested with no playlist loaded")
            return False

        data = await self._fetch_playlist(target)
        items = (data or {}).get("items") or []
        if not items:
            # Keeping a stale list beats dropping playback over a failed request
            # or an empty response, so this deliberately does not clear anything.
            logger.warning(
                "Playlist %s reload returned nothing usable — keeping the loaded list",
                target,
            )
            return False

        # The swap itself is held against the play lock: a track start that is
        # mid-flight picks its next track from the cursor, and must not see the
        # list and the cursor from two different versions of the playlist.
        async with self._play_lock:
            playing_id = self.current_track_id
            before = len(self.current_playlist)
            self._original_playlist = list(items)
            self.current_playlist_id = target
            if self._shuffle_enabled():
                self._reshuffle_playlist(keep_track_id=playing_id)
            else:
                self.current_playlist = items
            self._last_reload_at = time.monotonic()
            self._resolve_failures_since_reload = 0

            index = next(
                (i for i, item in enumerate(self.current_playlist) if str(item.get("id")) == str(playing_id)),
                None,
            )
            if index is not None:
                self.current_index = index
            else:
                # The track playing right now is gone upstream. It keeps playing
                # from VLC's own buffer; only the cursor needs to land somewhere
                # sane so the following track is picked from the new list.
                self.current_index = min(max(self.current_index, 0), len(self.current_playlist) - 1)

            logger.info(
                "Playlist %s reloaded: %d -> %d item(s); current track %s %s",
                target,
                before,
                len(items),
                playing_id,
                "kept in place" if index is not None else "no longer in the playlist",
            )

            # Anything prefetched now refers to the old ordering.
            if self._prefetch_task and not self._prefetch_task.done():
                self._prefetch_task.cancel()
            if self._expect_playing:
                self._schedule_prefetch_next()
        return True

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

    async def _play_current_track(self, *, _skip_depth: int = 0) -> None:
        """Serialised entry point for starting a track.

        Auto-advance runs from the VLC end-of-track event while dashboard commands
        run on the command worker, so two callers can reach this concurrently. Two
        overlapping set_media()/play() calls on one media player leave VLC wedged,
        so the whole start sequence is guarded by a lock.
        """
        async with self._play_lock:
            await self._play_current_track_locked(_skip_depth=_skip_depth)

    async def _play_current_track_locked(
        self, *, _skip_depth: int = 0, _fresh_retry: bool = False
    ) -> None:
        if not self.current_playlist or not self.player:
            return
        if _skip_depth >= min(len(self.current_playlist), 12):
            logger.error(
                "Too many unplayable tracks in a row (%s); stopping auto-advance",
                _skip_depth,
            )
            self._expect_playing = False
            return

        track = self.current_playlist[self.current_index]
        cached = None if _fresh_retry else self._get_cached_url(track)
        used_cached_url = cached is not None
        if cached:
            logger.info("Using prefetched URL for track %s", track.get("id"))
            media_url = cached
        else:
            media_url = await self._get_media_url(track, use_cache=False)
            if media_url:
                self._put_cached_url(track, media_url)
            else:
                self._mark_resolve_failed(track)

        if not media_url:
            logger.warning("No playable URL, skipping track %s", track.get("id"))
            self.current_index = (self.current_index + 1) % len(self.current_playlist)
            await self._play_current_track_locked(_skip_depth=_skip_depth + 1)
            return

        media = self.instance.media_new(media_url)
        # Media-level options stick more reliably than instance flags for HTTP/DASH.
        for opt in (
            ":network-caching=5000",
            ":file-caching=3000",
            ":http-reconnect",
            ":no-video",
        ):
            try:
                media.add_option(opt)
            except Exception:
                pass

        # Mute while VLC fills the network buffer, so a rough start is inaudible.
        target_vol = int(self.volume)
        try:
            self.player.audio_set_volume(0)
        except Exception:
            pass

        # Claim the track *before* awaiting the warmup: until these are set, a
        # concurrent play() for the same track sees a stale current_track_id and
        # cannot recognise it as a duplicate.
        self.current_track_id = track.get("id")
        self.current_track = track
        self._expect_playing = True
        self._stall_since = None
        self._play_started_at = time.monotonic()
        self._last_known_position_s = 0.0
        self._progress_position_ms = -1
        self._progress_changed_at = time.monotonic()
        self._seeked_at = 0.0

        # libvlc refuses to restart a player that has reached a terminal state:
        # set_media() + play() leave it sitting in Ended and nothing ever starts. Every
        # natural end-of-track transition hit this, which is why playback died a few
        # songs in and only came back ~10s later when the stall watchdog issued its own
        # stop(). Resetting here is what makes an unassisted transition work.
        await self._reset_player_for_new_media()

        self.player.set_media(media)
        # libvlc keeps its own reference once the media is attached to the player;
        # dropping ours here avoids leaking one media object per track.
        try:
            media.release()
        except Exception:
            pass
        self.player.play()

        warmed = await self._wait_for_playback_buffer(timeout_s=8.0)
        try:
            self.player.audio_set_volume(target_vol)
        except Exception:
            pass

        # A prefetched YouTube URL can be rejected by the time it is used (the CDN
        # link is only good for a while), and VLC then goes straight to Ended/Error
        # within a fraction of a second. Without handling that here the track is
        # silently skipped and the branch stays quiet until the watchdog notices.
        # _expect_playing guards against a stop/pause that landed during warmup: that
        # leaves VLC in the same state as a failure but must not advance the playlist.
        if self._expect_playing and self._failed_to_start():
            if used_cached_url and not _fresh_retry:
                logger.warning(
                    "Track %s would not open with the prefetched URL — re-resolving",
                    track.get("id"),
                )
                self._invalidate_cached_url(track)
                await self._play_current_track_locked(
                    _skip_depth=_skip_depth, _fresh_retry=True
                )
                return
            logger.error(
                "Track %s failed to start even with a fresh URL — skipping",
                track.get("id"),
            )
            self._mark_resolve_failed(track)
            self.current_index = (self.current_index + 1) % len(self.current_playlist)
            await self._play_current_track_locked(_skip_depth=_skip_depth + 1)
            return

        self._play_started_at = time.monotonic()
        self._progress_changed_at = time.monotonic()
        if warmed:
            logger.info("Playing track %s (buffer ready)", self.current_track_id)
        else:
            logger.warning(
                "Playing track %s (buffer warmup slow — may stutter briefly)",
                self.current_track_id,
            )
        # Resolve the *next* track in the background so song transitions are near-instant.
        self._schedule_prefetch_next()

    async def _reset_player_for_new_media(self, player: Any = None) -> None:
        """Return a media player to a state from which play() is honoured.

        stop() is asynchronous, so we also wait for libvlc to actually tear the old
        input down; starting new media while the previous one is still closing lets the
        tail of that teardown cancel the fresh playback.
        """
        player = player or self.player
        if not player or not vlc:
            return
        try:
            if player.get_state() == vlc.State.NothingSpecial:
                return
            player.stop()
        except Exception:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            try:
                if player.get_state() in (vlc.State.NothingSpecial, vlc.State.Stopped):
                    return
            except Exception:
                return

    def _failed_to_start(self) -> bool:
        """True when VLC gave up on the media instead of starting playback."""
        if not self.player or not vlc:
            return False
        try:
            state = self.player.get_state()
        except Exception:
            return False
        return state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped)

    async def _wait_for_playback_buffer(self, timeout_s: float = 12.0) -> bool:
        """Wait until VLC leaves Buffering and stays in Playing long enough to fill cache."""
        if not self.player or not vlc:
            await asyncio.sleep(0.5)
            return False
        started_at = time.monotonic()
        deadline = started_at + timeout_s
        stable_since: Optional[float] = None
        saw_playing = False
        saw_active = False
        while time.monotonic() < deadline:
            try:
                state = self.player.get_state()
            except Exception:
                await asyncio.sleep(0.2)
                continue
            if state in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):
                # play() is asynchronous, so until this media is seen opening the state
                # still describes the previous one. Treating that as failure aborted the
                # warmup after ~150ms and reported a bogus timeout.
                if saw_active or time.monotonic() - started_at >= 2.0:
                    return False
                await asyncio.sleep(0.05)
                continue
            if state in (vlc.State.Opening, vlc.State.Buffering):
                saw_active = True
                stable_since = None
            elif state == vlc.State.Playing:
                saw_active = True
                saw_playing = True
                # Prefer a bit of decoded audio in the pipe (pos>0) + stable Playing.
                try:
                    pos_ms = int(self.player.get_time() or 0)
                except Exception:
                    pos_ms = 0
                if pos_ms >= 400:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= 1.2:
                        return True
                else:
                    stable_since = None
            await asyncio.sleep(0.15)
        return saw_playing

    def _cache_key(self, track: Dict[str, Any]) -> str:
        return str(track.get("id") or track.get("source_id") or track.get("source_url") or "")

    def _get_cached_url(self, track: Dict[str, Any]) -> Optional[str]:
        key = self._cache_key(track)
        if not key:
            return None
        entry = self._url_cache.get(key)
        if not entry:
            return None
        url, expires_at = entry
        if time.monotonic() >= expires_at:
            self._url_cache.pop(key, None)
            return None
        return url

    def _put_cached_url(self, track: Dict[str, Any], url: str) -> None:
        key = self._cache_key(track)
        if key and url:
            self._url_cache[key] = (url, time.monotonic() + self._url_cache_ttl_s)
            self._failed_urls.pop(key, None)

    def _invalidate_cached_url(self, track: Dict[str, Any]) -> None:
        key = self._cache_key(track)
        if key:
            self._url_cache.pop(key, None)

    def _mark_resolve_failed(self, track: Dict[str, Any]) -> None:
        """Back off on tracks whose URL cannot be resolved.

        Without this, a permanently unavailable video (deleted, region-locked) makes
        ensure_next_prefetched() spawn a fresh yt-dlp process on every watchdog tick.
        Each spawn is a whole Python interpreter, which is heavy enough on a Pi to
        disturb the audio output of the track currently playing.
        """
        key = self._cache_key(track)
        if not key:
            return
        failures = self._failed_urls.get(key, (0, 0.0))[0] + 1
        backoff = min(self.RESOLVE_BACKOFF_BASE_S * (2 ** (failures - 1)), self.RESOLVE_BACKOFF_MAX_S)
        self._failed_urls[key] = (failures, time.monotonic() + backoff)
        logger.warning(
            "Track %s failed to resolve (attempt %s) — not retrying for %.0fs",
            track.get("id"),
            failures,
            backoff,
        )
        self._resolve_failures_since_reload += 1
        self._maybe_reload_after_failures()

    def _maybe_reload_after_failures(self) -> None:
        """Re-fetch the playlist once several tracks in a row cannot be resolved.

        The server sends a reload command when a refresh removes tracks, but that
        command can be missed while a device is offline. Repeated resolve failures
        are the symptom of exactly that, so the agent recovers on its own instead
        of paying a silence for every removed track until the next restart.
        """
        if self._resolve_failures_since_reload < self.RELOAD_AFTER_FAILURES:
            return
        if time.monotonic() - self._last_reload_at < self.RELOAD_COOLDOWN_S:
            return
        if not self.current_playlist_id:
            return
        loop = self._event_loop or asyncio.get_event_loop()
        # Fire and forget: this runs inside the play path, which must not wait on
        # an HTTP round trip before it moves on to the next track.
        self._last_reload_at = time.monotonic()
        self._resolve_failures_since_reload = 0
        logger.info(
            "%s tracks failed to resolve — re-fetching playlist %s in the background",
            self.RELOAD_AFTER_FAILURES,
            self.current_playlist_id,
        )
        loop.create_task(self.reload_playlist())

    def _resolve_backoff_active(self, track: Dict[str, Any]) -> bool:
        key = self._cache_key(track)
        if not key:
            return False
        entry = self._failed_urls.get(key)
        if not entry:
            return False
        if time.monotonic() >= entry[1]:
            return False
        return True

    def _next_track_index(self) -> Optional[int]:
        if not self.current_playlist:
            return None
        if self.repeat_mode == "repeat_one":
            return self.current_index
        if self.repeat_mode == "play_once":
            nxt = self.current_index + 1
            if nxt >= len(self.current_playlist):
                return None
            return nxt
        if self.repeat_mode == "single":
            return None
        # shuffle behaves like repeat_all over the already-randomized list
        return (self.current_index + 1) % len(self.current_playlist)

    def _schedule_prefetch_next(self) -> None:
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prefetch_task = loop.create_task(self._prefetch_next_track())

    async def _wait_until_prefetch_point(self) -> None:
        """Delay prefetching until the current track is most of the way through.

        Resolving right after a track starts produced URLs that were ~5 minutes old
        by the time they were used, and YouTube had already invalidated them. Waiting
        keeps the link fresh while still resolving well before the track ends.
        """
        deadline = time.monotonic() + self.PREFETCH_MAX_WAIT_S
        while time.monotonic() < deadline:
            if not self._expect_playing or self._ad_playing:
                return
            try:
                length_ms = self.player.get_length() if self.player else 0
                pos_ms = self.player.get_time() if self.player else 0
            except Exception:
                return
            # Unknown length (live/odd streams): fall back to the timeout.
            if length_ms and length_ms > 0 and pos_ms >= length_ms * self.PREFETCH_AT_FRACTION:
                return
            await asyncio.sleep(2.0)

    async def _prefetch_next_track(self) -> None:
        try:
            # Let the current track finish its buffer warmup before competing for CPU.
            await asyncio.sleep(5.0)
            await self._wait_until_prefetch_point()
            if not self.current_playlist or self._ad_playing:
                return
            nxt = self._next_track_index()
            if nxt is None:
                return
            track = self.current_playlist[nxt]
            if self._get_cached_url(track):
                logger.info("Next track %s already prefetched", track.get("id"))
                return
            if self._resolve_backoff_active(track):
                return
            logger.info("Prefetching next track %s", track.get("id"))
            url = await self._get_media_url(track, use_cache=False)
            if url:
                self._put_cached_url(track, url)
                logger.info("Prefetched next track %s", track.get("id"))
            else:
                self._mark_resolve_failed(track)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Prefetch failed: %s", exc)

    async def ensure_next_prefetched(self) -> None:
        """Call periodically near end-of-track to make sure next URL is ready."""
        if not self.player or not self._expect_playing or self._ad_playing:
            return
        if not self.current_playlist:
            return
        try:
            length_ms = self.player.get_length()
            pos_ms = self.player.get_time()
        except Exception:
            return
        if length_ms <= 0 or pos_ms < 0:
            return
        # Safety net for the scheduled prefetch: same trigger point, so a cancelled or
        # crashed prefetch task still gets the next URL cached before the track ends.
        if pos_ms < length_ms * self.PREFETCH_AT_FRACTION:
            return
        nxt = self._next_track_index()
        if nxt is None:
            return
        track = self.current_playlist[nxt]
        if self._get_cached_url(track):
            return
        if self._resolve_backoff_active(track):
            return
        if self._prefetch_task and not self._prefetch_task.done():
            return
        self._schedule_prefetch_next()

    async def recover_if_stalled(self, stall_seconds: float = 20.0) -> bool:
        """Recover playback that has died without VLC reporting Ended/Error.

        Two distinct failure modes are handled:

        1. VLC reports Ended/Error while we still expect audio -> advance.
        2. VLC still reports Playing/Buffering but the playback position stops
           moving. This is what happens when the HTTP source dies mid-track: the
           input socket is gone, libvlc never transitions out of Playing, and the
           device stays silent indefinitely while still reporting "playing" to the
           dashboard. Only a position check can see it.
        """
        if not self.player or not self._expect_playing or self._ad_playing:
            self._stall_since = None
            self._progress_changed_at = time.monotonic()
            return False
        if not self.current_playlist:
            return False
        # A start is already in flight; let it finish before judging progress.
        if self._play_lock.locked():
            self._progress_changed_at = time.monotonic()
            return False

        try:
            state = self.player.get_state()
        except Exception:
            return False

        stalled_states = {vlc.State.Ended, vlc.State.Error} if vlc else set()
        if state in stalled_states:
            now = time.time()
            if self._stall_since is None:
                self._stall_since = now
                return False
            if now - self._stall_since < stall_seconds:
                return False
            logger.warning(
                "Playback stalled (vlc_state=%s) for %.0fs — advancing to next track",
                getattr(state, "name", state),
                now - self._stall_since,
            )
            self._stall_since = None
            await self.next()
            return True

        self._stall_since = None
        return await self._recover_if_position_frozen(state)

    async def _recover_if_position_frozen(self, state: Any) -> bool:
        """Restart the current track if its position has stopped advancing."""
        # Don't fight the initial buffer fill — a fresh start legitimately sits at 0.
        if self._play_started_at and (time.monotonic() - self._play_started_at) < self.FROZEN_GRACE_S:
            return False
        # A seek re-fills the same buffer, so allow it the same grace as a start.
        if self._seeked_at and (time.monotonic() - self._seeked_at) < self.FROZEN_GRACE_S:
            return False
        try:
            pos_ms = int(self.player.get_time() or 0)
        except Exception:
            return False

        now = time.monotonic()
        # Treat anything beyond a poll-jitter allowance as real forward progress.
        if pos_ms < 0 or abs(pos_ms - self._progress_position_ms) > 250:
            self._progress_position_ms = pos_ms
            self._progress_changed_at = now
            # Healthy playback clears the give-up counter so an isolated freeze later
            # in the same track still gets its full quota of retries.
            if self._frozen_track_id is not None and pos_ms > 0:
                self._frozen_track_id = None
                self._frozen_restarts = 0
            return False

        frozen_for = now - self._progress_changed_at
        if frozen_for < self.FROZEN_TIMEOUT_S:
            return False

        self._progress_changed_at = now
        self._progress_position_ms = -1
        if self.current_index >= len(self.current_playlist):
            self.current_index = 0

        if self._frozen_track_id != self.current_track_id:
            self._frozen_track_id = self.current_track_id
            self._frozen_restarts = 0
        self._frozen_restarts += 1

        # Retrying the same track forever would leave the branch silent, so give up
        # on it after a couple of attempts and move on to the next one.
        give_up = self._frozen_restarts > self.FROZEN_MAX_RESTARTS
        logger.error(
            "Playback frozen at %.1fs for %.0fs (vlc_state=%s) on track %s — %s",
            pos_ms / 1000.0,
            frozen_for,
            getattr(state, "name", state),
            self.current_track_id,
            "skipping to next track" if give_up else f"restart attempt {self._frozen_restarts}",
        )
        # The cached stream URL is the most likely culprit, so force a fresh resolve.
        self._invalidate_cached_url(self.current_playlist[self.current_index])
        try:
            self.player.stop()
        except Exception:
            pass
        await asyncio.sleep(0.3)
        if give_up:
            self._frozen_track_id = None
            self._frozen_restarts = 0
            await self.next()
        else:
            await self._play_current_track()
        return True

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

        # Don't block the next track on analytics — log in the background.
        if self.on_track_ended and ended_track_id is not None:
            try:
                result = self.on_track_ended(
                    int(ended_track_id),
                    int(ended_playlist_id) if ended_playlist_id is not None else None,
                    duration_played,
                )
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
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
            current = self._intended_music_volume()
            self._ad_pre_music_volume = current
            try:
                self._ad_saved_position_ms = max(self.player.get_time(), 0)
            except Exception:
                self._ad_saved_position_ms = 0
            await self._fade_player_volume(self.player, current, 0, fade_out)
            return
        duck_percent = int(cfg.get("duck_music_volume_percent") or 25)
        duck_percent = max(5, min(duck_percent, 80))
        current = self._intended_music_volume()
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
                # Same libvlc constraint as music: a player left in a terminal state
                # ignores play(), and the stop() issued when the previous ad finished is
                # asynchronous, so make sure it has landed before reusing the player.
                await self._reset_player_for_new_media(self.ad_player)
                self.ad_player.set_media(media)
                try:
                    media.release()
                except Exception:
                    pass
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
                # The music finished underneath a ducked ad, so the player is
                # sitting in Ended: restoring its volume alone leaves the device
                # silent until the stall watchdog eventually advances the track.
                if overlay_mode == "duck" and after_action == "next" and self.current_playlist:
                    await self.next()
        except Exception as exc:
            logger.warning("Could not restore music after ad: %s", exc)

        self._ad_pre_music_volume = None
        self._ad_saved_position_ms = None

    async def _get_media_url(self, track: Dict[str, Any], *, use_cache: bool = True) -> Optional[str]:
        if use_cache:
            cached = self._get_cached_url(track)
            if cached:
                return cached

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
            resolved = await self._get_youtube_stream_url(str(youtube_url))
            if resolved:
                self._put_cached_url(track, resolved)
            return resolved

        if source_url:
            return self._absolutize_url(str(source_url))

        return None

    async def _get_youtube_stream_url(self, youtube_url: str) -> Optional[str]:
        # CRITICAL: yt-dlp is pure-Python and holds the GIL. asyncio.to_thread is NOT
        # enough — it freezes heartbeats/WS pings/commands on the Pi for minutes.
        # Run extraction in a separate *process* via the yt-dlp CLI instead.
        logger.info("Resolving YouTube URL via subprocess: %s", youtube_url)
        try:
            return await asyncio.wait_for(
                self._extract_youtube_url_subprocess(youtube_url),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.error("yt-dlp subprocess timed out for %s", youtube_url)
            return None
        except Exception as exc:
            logger.error("yt-dlp subprocess error: %s", exc)
            return None

    async def _extract_youtube_url_subprocess(self, youtube_url: str) -> Optional[str]:
        import sys

        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--quiet",
            "--no-warnings",
            "--no-playlist",
            "--skip-download",
            # Prefer AAC/m4a (140) over webm/opus (251): VLC on Pi often stutters on opus DASH.
            "-f",
            "140/bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
            "-g",
            youtube_url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace").strip()
            logger.error("yt-dlp exited %s: %s", proc.returncode, err[:500])
            return None
        lines = (stdout or b"").decode(errors="replace").strip().splitlines()
        for line in lines:
            url = line.strip()
            if url.startswith("http://") or url.startswith("https://"):
                return url
        return None
