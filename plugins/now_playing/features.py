"""
Song "feel" for the EQ styles: tempo, energy, danceability, loudness, acousticness.

Nothing gives the Pi the audio itself, so the EQ is driven by these per-song
numbers instead (see eq.py). Both services are free and need no key:
  ReccoBeats  /v1/audio-features?ids=<Spotify ID or ISRC> — the full set
  Deezer      search by artist + title → the track's ISRC (for ReccoBeats) and
              its own bpm / gain, used when ReccoBeats doesn't know the song

Lookups run on the plugin's update thread (never the draw path); results,
including "not found", are cached in memory and in data/now_playing/features.json.
"""

import json
import logging
import os
import re
import threading
import time

import requests

from utilities.paths import DATA_ROOT

log = logging.getLogger(__name__)

RECCOBEATS = "https://api.reccobeats.com/v1/audio-features"
DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_TRACK = "https://api.deezer.com/track/{}"
TIMEOUT = 6
MISS_RETRY_SECS = 24 * 3600       # retry a song that wasn't found after a day
NET_RETRY_SECS = 120              # …or after 2 min if the lookup itself failed
MAX_ENTRIES = 2000

DEFAULTS = {"tempo": 110.0, "energy": 0.6, "danceability": 0.6, "loudness": -8.0, "acousticness": 0.2}

_PATH = os.path.join(DATA_ROOT, "data", "now_playing", "features.json")
_cache: dict = {}                 # key → {"f": features | None, "t": epoch, "src": str}
_loaded = False
_lock = threading.Lock()
_HEADERS = {"User-Agent": "LEDMatrix/1.0"}


def track_key(track: dict) -> str:
    return f"{(track.get('artist') or '').lower()}|{(track.get('title') or '').lower()}"


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_PATH) as f:
            _cache.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save():
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        items = sorted(_cache.items(), key=lambda kv: kv[1].get("t", 0))[-MAX_ENTRIES:]
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(items), f)
        os.replace(tmp, _PATH)
    except OSError as e:
        log.debug(f"[now_playing] couldn't save features cache: {e}")


def _clean(s: str) -> str:
    """Drop '(feat. …)', '[Remastered 2011]', '- Radio Edit' etc. so searches match."""
    s = re.sub(r"\s*[\(\[][^)\]]*(feat|ft\.|remaster|edit|version|live|mono|stereo|deluxe)[^)\]]*[\)\]]", "",
               s or "", flags=re.I)
    s = re.sub(r"\s+-\s+.*(remaster|edit|version|mix|live).*$", "", s, flags=re.I)
    return s.strip()


def _reccobeats(ident: str):
    r = requests.get(RECCOBEATS, params={"ids": ident}, timeout=TIMEOUT, headers=_HEADERS)
    if r.status_code != 200:
        return None
    rows = (r.json() or {}).get("content") or []
    if not rows:
        return None
    row = rows[0]
    out = {k: row.get(k) for k in DEFAULTS}
    return out if out.get("tempo") else None


def _deezer_search(q: str) -> list:
    r = requests.get(DEEZER_SEARCH, params={"q": q, "limit": 1}, timeout=TIMEOUT, headers=_HEADERS)
    return ((r.json() or {}).get("data") or []) if r.status_code == 200 else []


def _deezer(track: dict):
    """(isrc, partial features) from Deezer, or (None, None)."""
    artist = _clean((track.get("artist") or "").split(",")[0])
    title = _clean(track.get("title") or "")
    if not title:
        return None, None
    q = f'artist:"{artist}" track:"{title}"' if artist else title
    hits = _deezer_search(q) or _deezer_search(f"{artist} {title}")
    if not hits:
        return None, None
    r = requests.get(DEEZER_TRACK.format(hits[0]["id"]), timeout=TIMEOUT, headers=_HEADERS)
    d = r.json() if r.status_code == 200 else {}
    partial = {}
    if d.get("bpm"):
        partial["tempo"] = float(d["bpm"])
    if d.get("gain") is not None:
        partial["loudness"] = float(d["gain"])
    return d.get("isrc"), partial or None


def _lookup(track: dict):
    """(features | None, source label). Raises on network errors."""
    if track.get("source") == "spotify" and re.fullmatch(r"[A-Za-z0-9]{22}", track.get("art_key") or ""):
        f = _reccobeats(track["art_key"])
        if f:
            return f, "ReccoBeats"
    isrc, partial = _deezer(track)
    if isrc:
        f = _reccobeats(isrc)
        if f:
            return f, "ReccoBeats"
    if partial:
        return partial, "Deezer"
    return None, ""


def lookup(track: dict):
    """Cached features for this track (fetching if needed), or None. Call off the draw path."""
    key = track_key(track)
    with _lock:
        _load()
        hit = _cache.get(key)
    now = time.time()
    if hit and (hit.get("f") or now - hit.get("t", 0) < hit.get("retry", MISS_RETRY_SECS)):
        return hit.get("f")
    try:
        f, src = _lookup(track)
        entry = {"f": f, "t": now, "src": src}
        log.info(f"[now_playing] EQ features for {track.get('artist')} — {track.get('title')}: "
                 f"{'from ' + src if f else 'not found'}")
    except Exception as e:
        log.debug(f"[now_playing] features lookup failed: {e}")
        entry = {"f": None, "t": now, "retry": NET_RETRY_SECS}
    with _lock:
        _cache[key] = entry
        _save()
    return entry["f"]


def cached(track: dict):
    """Features already looked up (no network), or None."""
    with _lock:
        _load()
        hit = _cache.get(track_key(track))
    return hit.get("f") if hit else None


def source_of(track: dict) -> str:
    with _lock:
        hit = _cache.get(track_key(track)) or {}
    return hit.get("src", "")


def with_defaults(f) -> dict:
    out = dict(DEFAULTS)
    for k, v in (f or {}).items():
        if v is not None:
            out[k] = float(v)
    return out
