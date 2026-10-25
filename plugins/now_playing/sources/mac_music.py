"""
Mac Music app (Apple Music streaming + the iTunes library) — read live over SSH.

The Pi runs `osascript` on the Mac through SSH; nothing is installed or kept
running on the Mac. One-time setup on the Mac: System Settings → General →
Sharing → Remote Login on, add this Pi's public key to ~/.ssh/authorized_keys,
and allow the first "control Music" Automation prompt.

The SSH key lives in data/now_playing/ (gitignored), so each Pi has its own.
A ControlMaster connection is kept open so 5 s polling stays cheap.
"""

import base64
import logging
import os
import subprocess

log = logging.getLogger(__name__)

_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "now_playing"))
KEY_PATH = os.path.join(_DIR, "id_ed25519")

# Only talks to Music if it's already running, so polling never launches the app.
TRACK_SCRIPT = '''
if application "Music" is running then
    tell application "Music"
        if player state is playing then
            set t to current track
            return (name of t) & tab & (artist of t) & tab & (album of t) & tab & ((duration of t) as text) & tab & ((player position) as text) & tab & (persistent ID of t)
        end if
    end tell
end if
return ""
'''

# Writes the current track's artwork to a temp file and prints its path ("" if none).
ART_SCRIPT = '''
tell application "Music"
    try
        set d to raw data of artwork 1 of current track
    on error
        try
            set d to data of artwork 1 of current track
        on error
            return ""
        end try
    end try
end tell
set f to (POSIX path of (path to temporary items)) & "led_now_playing_art"
set fh to open for access (POSIX file f) with write permission
set eof fh to 0
write d to fh
close access fh
return f
'''


class MacMusicError(Exception):
    pass


def ensure_key() -> str:
    """Create this Pi's SSH key if needed; return the public key line."""
    os.makedirs(_DIR, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "led-matrix-now-playing",
                        "-f", KEY_PATH], check=True, capture_output=True, timeout=30)
    with open(KEY_PATH + ".pub") as f:
        return f.read().strip()


def _ssh(host: str, user: str, remote_cmd: str, stdin: str, timeout: float = 8) -> str:
    if not host or not user:
        raise MacMusicError("Set the Mac hostname and username")
    if not os.path.exists(KEY_PATH):
        raise MacMusicError("No SSH key yet — click Show key in the config page and add it to the Mac")
    cmd = [
        "ssh", "-i", KEY_PATH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={os.path.join(_DIR, 'known_hosts')}",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={os.path.join(_DIR, 'cm-%r@%h')}",
        "-o", "ControlPersist=120",
        f"{user}@{host}", remote_cmd,
    ]
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A pending macOS "allow control of Music" prompt also blocks osascript until timeout
        raise MacMusicError(f"Timed out talking to {host} — if the Mac is showing an "
                            f"\"allow … to control Music\" prompt, click Allow")
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"ssh exited {r.returncode}"
        if "Permission denied" in msg:
            msg = "Permission denied — add this Pi's key to the Mac (Show key)"
        elif "Could not resolve" in msg or "No route" in msg or "timed out" in msg.lower():
            msg = f"Can't reach {host} — is the Mac awake and Remote Login on?"
        elif "-1743" in (r.stderr or "") or "Not authorized" in (r.stderr or ""):
            msg = "The Mac blocked control of Music — allow it in System Settings → Privacy & Security → Automation"
        raise MacMusicError(msg)
    return r.stdout


def parse_track(output: str):
    """osascript output → track dict, or None when nothing is playing."""
    line = (output or "").strip("\n")
    if not line.strip():
        return None
    parts = line.split("\t")
    if len(parts) < 6:
        raise MacMusicError(f"Unexpected Music output: {line[:80]!r}")
    name, artist, album, dur, pos, pid = parts[:6]

    def secs(v):
        try:
            return float(v.replace(",", "."))      # some locales use a decimal comma
        except ValueError:
            return None

    d, p = secs(dur), secs(pos)
    return {
        "source": "mac",
        "title": name,
        "artist": artist,
        "album": album,
        "art_key": pid,
        "progress_ms": int(p * 1000) if p is not None else None,
        "duration_ms": int(d * 1000) if d is not None else None,
    }


def now_playing(host: str, user: str):
    return parse_track(_ssh(host, user, "osascript -", TRACK_SCRIPT))


def artwork(host: str, user: str) -> bytes:
    """Current track's artwork bytes, or b"" if it has none."""
    out = _ssh(host, user, 'f=$(osascript -); if [ -n "$f" ]; then base64 < "$f"; fi', ART_SCRIPT, timeout=15)
    out = out.strip()
    return base64.b64decode(out) if out else b""
