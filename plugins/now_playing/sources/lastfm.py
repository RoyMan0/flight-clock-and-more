"""
Last.fm "now scrobbling" — catch-all for any device that scrobbles to Last.fm
(e.g. Apple Music on iPhone via a scrobbler app). Free API key + username.
Last.fm doesn't report playback position, so no progress bar is drawn.
"""

import logging

import requests

log = logging.getLogger(__name__)

API = "https://ws.audioscrobbler.com/2.0/"
# Last.fm's grey-star "no image" placeholder
_PLACEHOLDER = "2a96cbd8b46e442fc41c2b86b821562f"


class LastfmError(Exception):
    pass


def api_key() -> str:
    try:
        from core.config_manager import get_config
        return (get_config().get_secret("lastfm_api_key", "") or "").strip()
    except Exception:
        return ""


def parse_recent(data: dict):
    """user.getrecenttracks JSON → track dict when the newest track is playing now, else None."""
    tracks = ((data or {}).get("recenttracks") or {}).get("track") or []
    if isinstance(tracks, dict):
        tracks = [tracks]
    if not tracks:
        return None
    t = tracks[0]
    if (t.get("@attr") or {}).get("nowplaying") != "true":
        return None
    images = {i.get("size"): i.get("#text", "") for i in t.get("image") or []}
    art = images.get("extralarge") or images.get("large") or images.get("medium") or ""
    if _PLACEHOLDER in art:
        art = ""
    artist = (t.get("artist") or {}).get("#text") or (t.get("artist") or {}).get("name", "")
    album = (t.get("album") or {}).get("#text", "")
    return {
        "source": "lastfm",
        "title": t.get("name", ""),
        "artist": artist,
        "album": album,
        "art_url": art,
        "art_key": f"{artist}|{album}|{t.get('name', '')}",
        "progress_ms": None,
        "duration_ms": None,
    }


def now_playing(user: str):
    key = api_key()
    if not key or not user:
        raise LastfmError("Set your Last.fm API key and username")
    r = requests.get(API, timeout=10, params={
        "method": "user.getrecenttracks", "user": user, "api_key": key,
        "format": "json", "limit": 1,
    }, headers={"User-Agent": "LEDMatrix/1.0"})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in data:
        raise LastfmError(data.get("message") or f"Last.fm returned {r.status_code}")
    return parse_recent(data)
