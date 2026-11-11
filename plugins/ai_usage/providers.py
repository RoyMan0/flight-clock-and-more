"""
Usage fetchers for the AI Usage plugin.

Both providers use the subscription's own OAuth login (no paid API):
  claude  GET api.anthropic.com/api/oauth/usage   — what Claude Code's /usage shows
  codex   GET chatgpt.com/backend-api/wham/usage  — what Codex's /status shows
Both endpoints are undocumented and may change.

Credentials come from the CLIs' own files, created by logging in on this Pi:
  claude  ~/.claude/.credentials.json   (claude auth login)
  codex   ~/.codex/auth.json            (codex login --device-auth)
Access tokens are short-lived, and the refresh token rotates on every refresh,
so after refreshing the new tokens are written back to the same file (same
owner and mode) to keep the CLI's login valid. `claude setup-token` tokens
can't be used: they lack the user:profile scope the usage endpoint needs.

On a Mac (the desktop app), Claude Code keeps its login in the login Keychain
("Claude Code-credentials") instead of the file. That is read with `security`
(macOS asks once to allow it) and only read: Claude Code refreshes it whenever
it runs, and rewriting another app's Keychain item could lock Claude Code out.

fetch() returns {current_pct, current_reset, weekly_pct, weekly_reset} with
percentages 0-100 and resets as epoch seconds (or None).
"""

import base64
import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

TIMEOUT = 15
REFRESH_MARGIN = 300            # refresh when the access token has < 5 min left

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_DEFAULT_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code"]

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DEFAULT_PATHS = {
    "claude": ".claude/.credentials.json",
    "codex": ".codex/auth.json",
}
LOGIN_HINTS = {
    "claude": "run 'claude auth login' on the Pi",
    "codex": "run 'codex login --device-auth' on the Pi",
}
if sys.platform != "linux":                  # the desktop app, logged in on this computer
    LOGIN_HINTS = {
        "claude": "log in to Claude Code on this computer ('claude auth login' in a terminal)",
        "codex": "run 'codex login' in a terminal on this computer",
    }
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_RETRY_SECS = 3600       # after a refused or failed Keychain read, don't ask again for an hour
_keychain_blocked_until = 0.0


class LoginNeeded(Exception):
    """No usable login: credentials missing, or refresh/auth rejected."""


# ------------------------------------------------------------------
# Credentials files
# ------------------------------------------------------------------

def find_credentials(provider: str, configured: str = "") -> str:
    """Configured path, else ~/<default>, else /home/*/<default> (the service may run as root)."""
    if configured:
        path = os.path.expanduser(configured.strip())
        return path if os.path.isfile(path) else ""
    rel = DEFAULT_PATHS[provider]
    home = os.path.join(os.path.expanduser("~"), rel)
    if os.path.isfile(home):
        return home
    matches = sorted(glob.glob(os.path.join("/home", "*", rel)))
    return matches[0] if matches else ""


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_like(path: str, data: dict):
    """Atomic rewrite that keeps the original file's mode and (when root) owner."""
    st = os.stat(path)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if os.geteuid() == 0:
        os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)


def _iso_to_epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _jwt_exp(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


# ------------------------------------------------------------------
# Claude
# ------------------------------------------------------------------

def _claude_refresh(path: str, data: dict) -> dict:
    oauth = data.get("claudeAiOauth") or {}
    refresh = oauth.get("refreshToken")
    if not refresh:
        raise LoginNeeded("no refresh token")
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLAUDE_CLIENT_ID,
        "scope": " ".join(oauth.get("scopes") or CLAUDE_DEFAULT_SCOPES),
    }
    r = requests.post(CLAUDE_TOKEN_URL, json=body, timeout=TIMEOUT)
    if r.status_code in (400, 401, 403):
        raise LoginNeeded(f"token refresh rejected ({r.status_code})")
    r.raise_for_status()
    tok = r.json()
    oauth["accessToken"] = tok["access_token"]
    oauth["refreshToken"] = tok.get("refresh_token") or refresh
    if tok.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + float(tok["expires_in"])) * 1000)
    data["claudeAiOauth"] = oauth
    _write_json_like(path, data)
    log.info("[ai_usage] Claude token refreshed")
    return data


def _keychain_claude(interactive=False):
    """Claude Code's login from the macOS Keychain (None when there isn't one). After a refusal
    the background poll leaves it alone for an hour; the Test button (interactive) asks again."""
    global _keychain_blocked_until
    if not interactive and time.time() < _keychain_blocked_until:
        raise LoginNeeded("the Keychain didn't give Flight Clock Claude Code's login "
                          "(it asks again within the hour)")
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True, timeout=120)   # waits while macOS asks
    except (OSError, subprocess.TimeoutExpired) as e:
        _keychain_blocked_until = time.time() + KEYCHAIN_RETRY_SECS
        raise LoginNeeded(f"couldn't read the Keychain ({e.__class__.__name__})")
    if r.returncode == 44:                    # errSecItemNotFound: not logged in on this Mac
        return None
    if r.returncode != 0:                     # e.g. "Deny" in the macOS prompt
        _keychain_blocked_until = time.time() + KEYCHAIN_RETRY_SECS
        raise LoginNeeded("the Keychain didn't allow reading Claude Code's login — "
                          "click Test and choose Always Allow")
    try:
        return json.loads(r.stdout.strip())
    except ValueError:
        raise LoginNeeded("Claude Code's Keychain entry isn't in the expected format")


def _claude_keychain(interactive: bool) -> dict:
    data = _keychain_claude(interactive)
    if data is None:
        raise LoginNeeded(f"no Claude login — {LOGIN_HINTS['claude']}")
    oauth = data.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise LoginNeeded("no Claude login in the Keychain")
    if (oauth.get("expiresAt") or 0) / 1000 < time.time():
        # Read-only (see the top): Claude Code renews it the next time it runs
        raise LoginNeeded("Claude Code's login has expired — open Claude Code once to renew it")
    return _claude_usage(data, None, False)


def _claude(path: str, allow_refresh: bool) -> dict:
    data = _read_json(path)
    oauth = data.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise LoginNeeded("no Claude login in credentials file")
    if allow_refresh and (oauth.get("expiresAt") or 0) / 1000 < time.time() + REFRESH_MARGIN:
        data = _claude_refresh(path, data)
    return _claude_usage(data, path, allow_refresh)


def _claude_usage(data: dict, path, allow_refresh: bool) -> dict:
    def get(d):
        return requests.get(CLAUDE_USAGE_URL, timeout=TIMEOUT, headers={
            "Authorization": f"Bearer {d['claudeAiOauth']['accessToken']}",
            "anthropic-beta": CLAUDE_BETA,
            "Accept": "application/json",
        })

    r = get(data)
    if r.status_code == 401 and allow_refresh and path:
        data = _claude_refresh(path, data)
        r = get(data)
    if r.status_code in (401, 403):
        raise LoginNeeded(f"usage request rejected ({r.status_code})"
                          + ("" if path else " — open Claude Code once to renew its login"))
    r.raise_for_status()
    j = r.json()
    five, week = j.get("five_hour") or {}, j.get("seven_day") or {}
    return {
        "current_pct": five.get("utilization"),
        "current_reset": _iso_to_epoch(five.get("resets_at")),
        "weekly_pct": week.get("utilization"),
        "weekly_reset": _iso_to_epoch(week.get("resets_at")),
    }


# ------------------------------------------------------------------
# Codex (ChatGPT subscription)
# ------------------------------------------------------------------

def _codex_refresh(path: str, data: dict) -> dict:
    tokens = data.get("tokens") or {}
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise LoginNeeded("no refresh token")
    body = {
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": "openid profile email",
    }
    r = requests.post(CODEX_TOKEN_URL, json=body, timeout=TIMEOUT)
    if r.status_code in (400, 401, 403):
        raise LoginNeeded(f"token refresh rejected ({r.status_code})")
    r.raise_for_status()
    tok = r.json()
    for k in ("id_token", "access_token", "refresh_token"):
        if tok.get(k):
            tokens[k] = tok[k]
    data["tokens"] = tokens
    data["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json_like(path, data)
    log.info("[ai_usage] Codex token refreshed")
    return data


def _window_reset(w: dict):
    if w.get("reset_at"):
        return float(w["reset_at"])
    if w.get("reset_after_seconds") is not None:
        return time.time() + float(w["reset_after_seconds"])
    return None


def _codex(path: str, allow_refresh: bool) -> dict:
    data = _read_json(path)
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token"):
        raise LoginNeeded("no ChatGPT login in auth.json")
    exp = _jwt_exp(tokens["access_token"])
    if allow_refresh and exp and exp < time.time() + REFRESH_MARGIN:
        data = _codex_refresh(path, data)

    def get(d):
        t = d["tokens"]
        headers = {
            "Authorization": f"Bearer {t['access_token']}",
            "Accept": "application/json",
            "User-Agent": "codex_cli_rs",
        }
        if t.get("account_id"):
            headers["ChatGPT-Account-Id"] = t["account_id"]
        return requests.get(CODEX_USAGE_URL, headers=headers, timeout=TIMEOUT)

    r = get(data)
    if r.status_code == 401 and allow_refresh:
        data = _codex_refresh(path, data)
        r = get(data)
    if r.status_code in (401, 403):
        raise LoginNeeded(f"usage request rejected ({r.status_code})")
    r.raise_for_status()
    rl = r.json().get("rate_limit") or {}
    primary, secondary = rl.get("primary_window") or {}, rl.get("secondary_window") or {}
    return {
        "current_pct": primary.get("used_percent"),
        "current_reset": _window_reset(primary),
        "weekly_pct": secondary.get("used_percent"),
        "weekly_reset": _window_reset(secondary),
    }


# ------------------------------------------------------------------
# Public
# ------------------------------------------------------------------

_FETCHERS = {"claude": _claude, "codex": _codex}
# One fetch per provider at a time: the plugin poll and the config page's Test
# button must never refresh concurrently, or one would use a rotated-out token.
_LOCKS = {p: threading.Lock() for p in _FETCHERS}


def fetch(provider: str, credentials_path: str = "", allow_refresh: bool = True, interactive: bool = False) -> dict:
    """interactive: a person asked (the Test button), so the Mac may show its Keychain prompt again."""
    path = find_credentials(provider, credentials_path)
    if not path and provider == "claude" and sys.platform == "darwin" and not (credentials_path or "").strip():
        with _LOCKS[provider]:
            return _claude_keychain(interactive)               # Claude Code on this Mac
    if not path:
        raise LoginNeeded(f"no credentials file — {LOGIN_HINTS[provider]}")
    with _LOCKS[provider]:
        return _FETCHERS[provider](path, allow_refresh)
