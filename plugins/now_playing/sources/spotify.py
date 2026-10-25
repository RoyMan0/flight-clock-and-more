"""
Spotify — what's playing on any of the user's devices (phone, computer,
speakers, car) via the Spotify Web API.

Auth: Authorization Code with PKCE (no client secret). Spotify only allows
plain-http redirect URIs on a loopback IP, so the redirect is
http://127.0.0.1:8888/callback. The browser can't load that page; the user
pastes the address-bar URL back into the config page, which exchanges the code.
The refresh token is stored in secrets.json (spotify_refresh_token); Spotify
may rotate it on refresh, so every new one is saved.
"""

import base64
import hashlib
import logging
import secrets as _secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-currently-playing user-read-playback-state"

_lock = threading.Lock()
_access = {"token": None, "expires": 0.0}
_pending = {}            # state → (verifier, created_at) for in-progress Connect flows


class SpotifyError(Exception):
    pass


def _cfg():
    from core.config_manager import get_config
    return get_config()


def client_id() -> str:
    return (_cfg().get_secret("spotify_client_id", "") or "").strip()


def refresh_token() -> str:
    return (_cfg().get_secret("spotify_refresh_token", "") or "").strip()


# ------------------------------------------------------------------
# Connect flow
# ------------------------------------------------------------------

def auth_url() -> str:
    cid = client_id()
    if not cid:
        raise SpotifyError("Save your Spotify Client ID first")
    verifier = _secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for k in [k for k, (_v, t) in _pending.items() if now - t > 600]:
            del _pending[k]
        _pending[state] = (verifier, now)
    return AUTH_URL + "?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256",
        "code_challenge": challenge,
    })


def parse_callback(pasted: str):
    """(code, state) from the pasted redirect URL (or just its query string)."""
    pasted = (pasted or "").strip()
    query = urlparse(pasted).query if "://" in pasted else pasted.lstrip("?")
    q = parse_qs(query)
    if "error" in q:
        raise SpotifyError(f"Spotify said: {q['error'][0]}")
    code, state = q.get("code", [""])[0], q.get("state", [""])[0]
    if not code:
        raise SpotifyError("That URL has no ?code= in it — paste the full address from the page that wouldn't load")
    return code, state


def complete_auth(pasted: str) -> str:
    """Exchange the pasted callback URL's code for tokens. Returns the account display name."""
    code, state = parse_callback(pasted)
    with _lock:
        entry = _pending.pop(state, None)
    if entry is None:
        raise SpotifyError("This sign-in link expired or came from another browser tab — click Connect again")
    r = requests.post(TOKEN_URL, timeout=10, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": client_id(), "code_verifier": entry[0],
    })
    if r.status_code != 200:
        raise SpotifyError(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    _store_tokens(r.json())
    return account_name()


def _store_tokens(tok: dict):
    with _lock:
        _access["token"] = tok.get("access_token")
        _access["expires"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    new_refresh = tok.get("refresh_token")
    if new_refresh and new_refresh != refresh_token():
        _cfg().save_secrets({"spotify_refresh_token": new_refresh})


def _token() -> str:
    with _lock:
        if _access["token"] and time.time() < _access["expires"]:
            return _access["token"]
    rt, cid = refresh_token(), client_id()
    if not rt or not cid:
        raise SpotifyError("Spotify not connected")
    r = requests.post(TOKEN_URL, timeout=10, data={
        "grant_type": "refresh_token", "refresh_token": rt, "client_id": cid,
    })
    if r.status_code != 200:
        raise SpotifyError(f"Token refresh failed ({r.status_code}) — reconnect Spotify in the config page")
    _store_tokens(r.json())
    return _access["token"]


def _get(path: str, **params):
    r = requests.get(API + path, params=params, timeout=10,
                     headers={"Authorization": f"Bearer {_token()}"})
    if r.status_code == 401:                      # token revoked/expired early — retry once
        with _lock:
            _access["token"] = None
        r = requests.get(API + path, params=params, timeout=10,
                         headers={"Authorization": f"Bearer {_token()}"})
    return r


def account_name() -> str:
    r = _get("/me")
    if r.status_code != 200:
        raise SpotifyError(f"Spotify /me failed ({r.status_code})")
    me = r.json()
    return me.get("display_name") or me.get("id") or "Spotify user"


def is_connected() -> bool:
    return bool(client_id() and refresh_token())


# ------------------------------------------------------------------
# Now playing
# ------------------------------------------------------------------

def parse_currently_playing(data: dict):
    """Spotify currently-playing JSON → track dict, or None when nothing is playing."""
    if not data or not data.get("is_playing") or not data.get("item"):
        return None
    item = data["item"]
    if data.get("currently_playing_type") == "episode" or item.get("type") == "episode":
        show = item.get("show") or {}
        artist, album = show.get("name", ""), show.get("publisher", "")
        images = item.get("images") or show.get("images") or []
    else:
        artist = ", ".join(a.get("name", "") for a in item.get("artists", []))
        album_obj = item.get("album") or {}
        album, images = album_obj.get("name", ""), album_obj.get("images") or []
    # Smallest image that's still ≥ 64 px (we scale to 22 px)
    sized = sorted((i for i in images if i.get("url")), key=lambda i: i.get("width") or 0)
    art = next((i["url"] for i in sized if (i.get("width") or 0) >= 64), sized[-1]["url"] if sized else "")
    return {
        "source": "spotify",
        "title": item.get("name", ""),
        "artist": artist,
        "album": album,
        "art_url": art,
        "art_key": item.get("id") or art,
        "progress_ms": data.get("progress_ms"),
        "duration_ms": item.get("duration_ms"),
    }


def now_playing():
    """Track dict for what's playing on any Spotify device, or None."""
    r = _get("/me/player/currently-playing", additional_types="episode")
    if r.status_code == 204:
        return None
    if r.status_code != 200:
        raise SpotifyError(f"currently-playing failed ({r.status_code})")
    return parse_currently_playing(r.json())
