"""
Checks for a newer Flight Clock about once a day and tells the person:
  - a banner on every settings page (base.html, through web/app.py's context processor)
  - one email per update, to the Alert Email (flights.email), unless flights.email_updates is off
  - on the Mac/PC app, the shell's own notice too (the on_new callback)

What "newer" means depends on where it runs:
  - Pi: commits on GitHub that this clone doesn't have yet (git fetch); updating is the
    Update button (POST /api/system/update).
  - Desktop app: the app registers a checker (set_checker) that reads its GitHub releases
    and returns {"version", "url"}; updating means downloading the new installer.

Never raises into the caller and never touches the display path.
"""

import json
import logging
import os
import socket
import subprocess
import threading
import time

from utilities.paths import APP_ROOT, DATA_ROOT, DESKTOP

log = logging.getLogger(__name__)

FIRST_CHECK_SECS = 90             # after startup: the network is up, and it doesn't slow the start
CHECK_EVERY_SECS = 24 * 3600
STATE_FILE = os.path.join(DATA_ROOT, "data", "update_check.json")   # only what's been emailed

_checker = None                   # desktop: fn() -> {"version", "url"} | None (raises on failure)
_on_new = None                    # desktop: fn(info), called once per newly found update
_status = {"available": False}
_lock = threading.Lock()
_started = False


def set_checker(fn, on_new=None):
    """Desktop app: how to look for a release, and what to do when one turns up."""
    global _checker, _on_new
    _checker, _on_new = fn, on_new


def status() -> dict:
    """The last result: {"available": bool, "kind": "pi"|"app", "count", "version", "url", "checked_at"}."""
    return dict(_status)


# --- the checks -----------------------------------------------------------

def _git(*args, timeout=15):
    return subprocess.run(["git", *args], cwd=APP_ROOT, capture_output=True, text=True, timeout=timeout)


def _check_pi():
    """Commits upstream that this clone hasn't pulled. Returns the status dict (raises on failure)."""
    fetch = _git("fetch", "--quiet", timeout=30)
    if fetch.returncode != 0:
        raise RuntimeError(fetch.stderr.strip() or "git fetch failed")
    count = int(_git("rev-list", "HEAD..@{u}", "--count").stdout.strip() or "0")
    info = {"available": count > 0, "kind": "pi", "count": count}
    if count:
        info["key"] = _git("rev-parse", "@{u}").stdout.strip()
        info["changes"] = [s for s in _git("log", "--format=%s", "-n", "8", "HEAD..@{u}").stdout.splitlines() if s]
    return info


def _check_app():
    found = _checker()
    if not found:
        return {"available": False, "kind": "app"}
    return {"available": True, "kind": "app", "version": found["version"], "url": found["url"],
            "key": found["version"]}


def check_now() -> dict:
    """Check right away (also the dashboard's and the app menu's Check for Updates). Returns the
    new status; on failure keeps the previous one and adds "error"."""
    global _status
    with _lock:
        try:
            info = _check_app() if (DESKTOP and _checker) else _check_pi()
        except Exception as e:
            log.info(f"[updates] check failed: {e}")
            return {**_status, "error": str(e)}
        info["checked_at"] = int(time.time())
        was = _status
        _status = info
    if info["available"]:
        if not was.get("available") or was.get("key") != info.get("key"):
            log.info(f"[updates] update available: {info.get('version') or str(info['count']) + ' changes'}")
        _notify(info)
    elif info["kind"] == "pi" and _load_state().get("notified") == "pi":
        _save_state({**_load_state(), "notified": None})     # installed: the next update emails again
    return info


def mark_installed():
    """The Pi's Update button pulled everything: drop the banner until the next check."""
    global _status
    _status = {"available": False, "kind": "pi", "checked_at": int(time.time())}


# --- telling the person -----------------------------------------------------

def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.debug(f"[updates] couldn't save state: {e}")


def _notify(info):
    """Once per update: the app's own notice, and an email if there's an address. The Pi pulls
    every commit, so it emails once when updates appear and not again until they're installed
    (key = "pi"), rather than once per commit."""
    state = _load_state()
    key = info.get("key") if info["kind"] == "app" else "pi"
    if state.get("notified") == key:
        return
    state["notified"] = key
    _save_state(state)
    if _on_new:
        try:
            _on_new(info)
        except Exception:
            log.exception("[updates] app notice failed")
    threading.Thread(target=_email, args=(info,), daemon=True, name="update-email").start()


def _email(info):
    try:
        from core.config_manager import get_config
        flights = get_config().get("flights") or {}
        to = (flights.get("email") or "").strip()
        if not to or flights.get("email_updates") is False:
            return
        from setup import email_alerts
        subject, body = _message(info)
        if email_alerts._send(subject, body, receiver=to):
            log.info("[updates] emailed the update notice")
    except Exception as e:
        log.info(f"[updates] update email not sent: {e}")


def _message(info):
    if info["kind"] == "app":
        from utilities.paths import DESKTOP_OS
        pc = "PC" if DESKTOP_OS == "windows" else "Mac"
        return (f"Flight Clock {info['version']} is available",
                f"A new version of Flight Clock for your {pc} is out: {info['version']}.\n\n"
                f"Download it here:\n{info['url']}\n\n"
                f"Install it over the old one; your settings are kept.\n\n"
                f"(You're getting this because an Alert Email is set in Flight Clock's settings. "
                f"Turn off \"Email me about updates\" there to stop these.)")
    host = socket.gethostname()
    try:
        from core.address_card import local_ip
        ip = local_ip()
    except Exception:
        ip = ""
    where = f"http://{ip}:8080" if ip else "your clock's settings page"
    changes = "\n".join(f"  - {c}" for c in info.get("changes", [])[:8])
    n = info["count"]
    return ("Flight Clock update available",
            f"Your Flight Clock ({host}) has {n} update{'s' if n != 1 else ''} waiting.\n\n"
            f"To install: open {where} on your home Wi-Fi and click \"Update now\" in the "
            f"banner at the top (or Dashboard → Check for Updates). The clock restarts by itself.\n\n"
            + (f"What's new:\n{changes}\n\n" if changes else "")
            + "(You're getting this because an Alert Email is set in the clock's settings. "
              "Turn off \"Email me about updates\" there to stop these.)")


# --- the loop ----------------------------------------------------------------

def start():
    """Start the daily check (once per process)."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(FIRST_CHECK_SECS)
        while True:
            check_now()
            time.sleep(CHECK_EVERY_SECS)
    threading.Thread(target=loop, daemon=True, name="update-check").start()
