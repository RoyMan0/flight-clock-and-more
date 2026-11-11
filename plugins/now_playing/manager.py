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
  style          "art" (artwork + details), "eq" (EQ bars + progress),
                 "eq_text" (smaller EQ + one tiny scrolling title/artist line + progress),
                 "eq_mirror", "eq_wave" (oscilloscope) or "eq_burst" (radial spokes).
                 The EQ is estimated from the song's tempo/energy (features.py, eq.py).
                 Desktop app only: "viz_<preset>" — the panel window draws a full-resolution
                 visualizer (desktop/panel/js/viz.js); the LED frame shows "eq" meanwhile.
Secrets: spotify_client_id, spotify_refresh_token, lastfm_api_key

Takes its normal turn in the rotation while a song is playing, or paused (Spotify and
the Music app report pauses) within PAUSE_KEEP_SECS of music last playing, so a song
paused for a phone call stays up. Skipped when nothing is playing.
"""

import logging
import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

from plugins.base_plugin import BasePlugin
from plugins.now_playing import features
from plugins.now_playing.eq import Analyzer, TinyMarquee, render_burst, render_eq, render_mirror, render_wave
from plugins.now_playing.render import MATRIX_W, draw_paused, make_marquees, prepare_art, render_frame
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

SOURCES = ("spotify", "mac", "lastfm")
SOURCE_NAMES = {"spotify": "Spotify", "mac": "Mac Music app", "lastfm": "Last.fm"}
STALE_SECS = 30                 # forget a track if updates stop arriving
PAUSE_KEEP_SECS = 30 * 60       # show a paused song this long after music last played; a session
                                # that was already paused (e.g. Spotify's from yesterday) never shows
SOURCE_RETRY_SECS = 60          # skip a source this long after it errors (e.g. Spotify
                                # while the internet is down) so the next one is reached fast
_source_skip_until: dict = {}   # source → time.monotonic() it may be tried again
DEFAULT_SCROLL_SPEED = 1.0
STYLES = ("art", "eq", "eq_text", "eq_mirror", "eq_wave", "eq_burst")
VIZ_PRESETS = ("cycle", "vortex", "spectrum", "reflection", "rays", "kaleido", "plasma", "spiral")

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
    """Check enabled sources in order; return the first playing track (with art), else the first
    paused one, else None. If `statuses` is given, a status line per checked source is appended."""
    paused = None
    for source in enabled_sources(config):
        name = SOURCE_NAMES[source]
        # The config page's Test (statuses given) always tries every source
        if statuses is None and time.monotonic() < _source_skip_until.get(source, 0):
            continue
        try:
            track = fetch_source(config, source)
            _source_skip_until.pop(source, None)
        except Exception as e:
            log.debug(f"[now_playing] {source}: {e}")
            _source_skip_until[source] = time.monotonic() + SOURCE_RETRY_SECS
            if statuses is not None:
                statuses.append({"source": source, "name": name, "ok": False, "text": str(e)})
            continue
        if track and track.get("paused"):
            if statuses is not None:
                statuses.append({"source": source, "name": name, "ok": True,
                                 "text": f"Paused: {track['title']} — {track['artist']}"})
            paused = paused or track
            continue
        if track:
            attach_art(config, track)
            if statuses is not None:
                statuses.append({"source": source, "name": name, "ok": True,
                                 "text": f"Playing: {track['title']} — {track['artist']}"})
            return track
        if statuses is not None:
            statuses.append({"source": source, "name": name, "ok": True, "text": "Nothing playing"})
    if paused:
        attach_art(config, paused)
    return paused


CONTROLLABLE = ("spotify", "mac")   # sources that can change the song (Last.fm only reports it)
ACTIONS = ("next", "previous", "playpause")


def control(config: dict, track: dict, action: str, local=None):
    """Next / previous song or play-pause for the current track's source. `local(source, action)`
    is the desktop app's hook (app.media_cb): it controls a player on the same computer and
    returns True if it did, so Spotify works there without the Web API. Raises with a readable message."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    source = (track or {}).get("source")
    if source not in CONTROLLABLE:
        raise RuntimeError(f"{SOURCE_NAMES.get(source, 'This source')} can't control playback")
    if local and local(source, action):
        return
    if source == "spotify":
        from plugins.now_playing.sources import spotify
        spotify.control(action, paused=bool(track.get("paused")))
    else:
        from plugins.now_playing.sources import mac_music
        mac_music.control(config.get("mac_host", "").strip(), config.get("mac_user", "").strip(), action)


def style_of(config: dict) -> str:
    s = config.get("style", "art")
    if s.startswith("viz_") and s[4:] in VIZ_PRESETS:
        return s                                      # desktop visualizer (the LED frame draws "eq")
    return s if s in STYLES else "art"


def song_seconds(track: dict, now: float) -> float:
    """Playback position for the EQ's beat grid; sources without one count from when we first saw it."""
    if track.get("progress_ms") is not None:
        return track["progress_ms"] / 1000.0 + (0 if track.get("paused") else now - track.get("fetched_at", now))
    return now - track.get("first_seen", now)


def progress_fraction(track: dict, now: float):
    """0..1 interpolated since the last fetch (held while paused), or None when the source has no duration."""
    dur, pos = track.get("duration_ms"), track.get("progress_ms")
    if not dur or pos is None:
        return None
    if not track.get("paused"):
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
        self._eq = None                   # (track key, has features) → Analyzer
        self._eq_key = None
        self._tiny = None
        self._hue = 0.0
        self._trail = None                # eq_wave's fading phosphor trail
        self._last_playing_at = None      # monotonic time a source was last seen playing (see PAUSE_KEEP_SECS)

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
        if self._tiny:
            self._tiny.reset()

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        with self._lock:
            self._track = None
        self._marquees = self._marquee_key = None
        self._eq = self._eq_key = self._tiny = None

    @property
    def update_interval(self) -> float:
        return float(self.config.get("update_interval", 5))

    def update(self):
        errors = []
        track = first_playing(self.config, errors)
        for s in errors:
            if not s["ok"]:
                log.debug(f"[now_playing] {s['source']}: {s['text']}")
        if track and track.get("paused"):
            # Keep a song paused mid-listen; ignore sessions that were already paused
            last = self._last_playing_at
            if last is None or time.monotonic() - last > PAUSE_KEEP_SECS:
                track = None
        elif track:
            self._last_playing_at = time.monotonic()
        if track:
            track["fetched_at"] = time.monotonic()
            with self._lock:
                prev = self._track
            same = prev and features.track_key(prev) == features.track_key(track)
            track["first_seen"] = prev.get("first_seen", track["fetched_at"]) if same else track["fetched_at"]
            if style_of(self.config) != "art":
                track["features"] = features.lookup(track)     # network only once per song
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

    def current_art(self):
        """The current track's artwork (small PIL image) for /api/display/asset/art."""
        track = self._current()
        return track.get("art") if track else None

    def describe(self):
        track = self._current()
        if not track:
            return None
        out = {k: track.get(k) for k in ("title", "artist", "album", "source", "art_url", "art_key")} | {
            "has_art": track.get("art") is not None,
            "can_skip": track.get("source") in CONTROLLABLE,
            "paused": bool(track.get("paused")),
            "progress": progress_fraction(track, time.monotonic()),
            "style": style_of(self.config),
        }
        an = self._eq
        if out["style"] != "art" and an is not None:
            out["eq"] = [round(v, 2) for v in an.shown]
            out["eq_peaks"] = [round(v, 2) for v in an.peaks]
        if out["style"] != "art":
            # The desktop visualizers run the same analyser in JS at full frame rate
            out["features"] = features.with_defaults(track.get("features"))
            out["position"] = round(song_seconds(track, time.monotonic()), 2)
            out["seed"] = hash(features.track_key(track)) & 0xFFFF
        return out

    def _draw_eq(self, track: dict, style: str):
        now = time.monotonic()
        key = (features.track_key(track), track.get("features") is not None)
        if key != self._eq_key:
            self._eq = Analyzer(track.get("features"), seed=hash(key[0]) & 0xFFFF)
            self._eq_key = key
            self._tiny = TinyMarquee(track)
        paused = bool(track.get("paused"))
        self._eq.levels(song_seconds(track, now), playing=not paused)   # paused: the bars fall
        if not paused:
            self._hue = (self._hue + 0.0015) % 1.0
        progress = progress_fraction(track, now)
        if style == "eq_mirror":
            frame = render_mirror(self._eq, progress, self._hue)
        elif style == "eq_wave":
            frame, self._trail = render_wave(self._eq, now, progress, self._trail, self._hue)
        elif style == "eq_burst":
            frame = render_burst(self._eq, now, progress, self._hue)
        else:                                         # eq, eq_text, and desktop viz_* styles
            frame = render_eq(self._eq, progress, self._hue, self._tiny if style == "eq_text" else None)
        if paused:
            draw_paused(frame, center=(MATRIX_W // 2, 12))
        push_pil_to_canvas(frame, self.display_manager)
        if style == "eq_text":
            self._tiny.tick(self._scroll_speed())
        return True

    def draw(self) -> bool:
        track = self._current()
        if not track:
            return False
        style = style_of(self.config)
        if style != "art":
            return self._draw_eq(track, style)
        key = (track["source"], track["title"], track["artist"], track["album"])
        if key != self._marquee_key:
            self._marquees = make_marquees(track)
            self._marquee_key = key
        frame = render_frame(*self._marquees, track.get("art"), progress_fraction(track, time.monotonic()))
        if track.get("paused"):
            draw_paused(frame)
        push_pil_to_canvas(frame, self.display_manager)
        speed = self._scroll_speed()
        for m in self._marquees:
            m.tick(speed)
        return True
