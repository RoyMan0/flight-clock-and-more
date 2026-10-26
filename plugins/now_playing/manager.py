"""
NowPlayingPlugin — the song that's playing right now: title scrolling across
the top, artwork bottom-left, artist and album beside it, progress bar.

Sources (checked in the user's order; the first one playing wins):
  spotify  Spotify Web API — any of the user's devices
  mac      Music app on the user's Mac, read over SSH (Apple Music + iTunes library)
  lastfm   Last.fm "now scrobbling" — anything that scrobbles

Config (plugins.now_playing):
  source_order   e.g. ["spotify", "mac", "lastfm"]
  sources        {"spotify": true, "mac": false, "lastfm": false}
  mac_host, mac_user, lastfm_user
  scroll_speed   px/frame for long lines
Secrets: spotify_client_id, spotify_refresh_token, lastfm_api_key

Takes its normal turn in the rotation; skipped when nothing is playing.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

from plugins.base_plugin import BasePlugin
from plugins.now_playing.render import make_marquees, prepare_art, render_frame
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

SOURCES = ("spotify", "mac", "lastfm")
SOURCE_NAMES = {"spotify": "Spotify", "mac": "Mac Music app", "lastfm": "Last.fm"}
STALE_SECS = 30                 # forget a track if updates stop arriving
DEFAULT_SCROLL_SPEED = 1.0

_art_cache: "OrderedDict[str, object]" = OrderedDict()   # art_key → 21×21 image (or None)
_art_lock = threading.Lock()


# ------------------------------------------------------------------
# Shared helpers (also used by the config page's Test button)
# ------------------------------------------------------------------

def source_order(config: dict) -> list:
    order = [s for s in config.get("source_order") or [] if s in SOURCES]
    return order + [s for s in SOURCES if s not in order]


def enabled_sources(config: dict) -> list:
    on = config.get("sources") or {"spotify": True}
    return [s for s in source_order(config) if on.get(s)]


def fetch_source(config: dict, source: str):
    """Track dict if `source` is playing now, else None. Raises on setup/network errors."""
    if source == "spotify":
        from plugins.now_playing.sources import spotify
        return spotify.now_playing()
    if source == "mac":
        from plugins.now_playing.sources import mac_music
        return mac_music.now_playing(config.get("mac_host", "").strip(), config.get("mac_user", "").strip())
    if source == "lastfm":
        from plugins.now_playing.sources import lastfm
        return lastfm.now_playing(config.get("lastfm_user", "").strip())
    return None


def attach_art(config: dict, track: dict):
    """Fill track["art"] (21×21 image or None), fetching each track's artwork only once."""
    key = f"{track['source']}:{track.get('art_key', '')}"
    with _art_lock:
        if key in _art_cache:
            _art_cache.move_to_end(key)
            track["art"] = _art_cache[key]
            return
    data = b""
    try:
        if track["source"] == "mac":
            from plugins.now_playing.sources import mac_music
            data = mac_music.artwork(config.get("mac_host", "").strip(), config.get("mac_user", "").strip())
        elif track.get("art_url"):
            r = requests.get(track["art_url"], timeout=10, headers={"User-Agent": "LEDMatrix/1.0"})
            if r.status_code == 200:
                data = r.content
    except Exception as e:
        log.debug(f"[now_playing] artwork fetch failed ({track['source']}): {e}")
    art = prepare_art(data)
    with _art_lock:
        _art_cache[key] = art
        while len(_art_cache) > 30:
            _art_cache.popitem(last=False)
    track["art"] = art


def first_playing(config: dict, statuses: list = None):
    """Check enabled sources in order; return the first playing track (with art) or None.
    If `statuses` is given, a status line per checked source is appended to it."""
    for source in enabled_sources(config):
        name = SOURCE_NAMES[source]
        try:
            track = fetch_source(config, source)
        except Exception as e:
            log.debug(f"[now_playing] {source}: {e}")
            if statuses is not None:
                statuses.append({"source": source, "name": name, "ok": False, "text": str(e)})
            continue
        if track:
            attach_art(config, track)
            if statuses is not None:
                statuses.append({"source": source, "name": name, "ok": True,
                                 "text": f"Playing: {track['title']} — {track['artist']}"})
            return track
        if statuses is not None:
            statuses.append({"source": source, "name": name, "ok": True, "text": "Nothing playing"})
    return None


def progress_fraction(track: dict, now: float):
    """0..1 interpolated since the last fetch, or None when the source has no duration."""
    dur, pos = track.get("duration_ms"), track.get("progress_ms")
    if not dur or pos is None:
        return None
    pos += (now - track.get("fetched_at", now)) * 1000
    return max(0.0, min(1.0, pos / dur))


# ------------------------------------------------------------------
# Plugin
# ------------------------------------------------------------------

class NowPlayingPlugin(BasePlugin):
    PLUGIN_ID = "now_playing"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._track: Optional[dict] = None
        self._marquees = None
        self._marquee_key = None

    @property
    def enabled(self) -> bool:
        # Off unless configured: installs from before this module have no config
        # block, and the update loop polls every enabled plugin even when it isn't
        # in plugin_order.
        return bool(self.config.get("enabled", False))

    def reset(self):
        if self._marquees:
            for m in self._marquees:
                m.reset()

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        with self._lock:
            self._track = None
        self._marquees = self._marquee_key = None

    @property
    def update_interval(self) -> float:
        return float(self.config.get("update_interval", 5))

    def update(self):
        errors = []
        track = first_playing(self.config, errors)
        for s in errors:
            if not s["ok"]:
                log.debug(f"[now_playing] {s['source']}: {s['text']}")
        if track:
            track["fetched_at"] = time.monotonic()
        with self._lock:
            self._track = track

    def _current(self) -> Optional[dict]:
        with self._lock:
            t = self._track
        if t and time.monotonic() - t["fetched_at"] <= STALE_SECS:
            return t
        return None

    def has_content(self) -> bool:
        return self._current() is not None

    def _scroll_speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("scroll_speed", DEFAULT_SCROLL_SPEED)))
        except (TypeError, ValueError):
            return DEFAULT_SCROLL_SPEED

    def draw(self) -> bool:
        track = self._current()
        if not track:
            return False
        key = (track["source"], track["title"], track["artist"], track["album"])
        if key != self._marquee_key:
            self._marquees = make_marquees(track)
            self._marquee_key = key
        frame = render_frame(*self._marquees, track.get("art"), progress_fraction(track, time.monotonic()))
        push_pil_to_canvas(frame, self.display_manager)
        speed = self._scroll_speed()
        for m in self._marquees:
            m.tick(speed)
        return True
