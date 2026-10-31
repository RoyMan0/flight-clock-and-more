"""
Mac Music app (Apple Music streaming + the iTunes library) — read live over SSH.

The Pi runs `osascript` on the Mac through SSH; nothing is installed or kept
running on the Mac. One-time setup on the Mac: System Settings → General →
Sharing → Remote Login on, add this Pi's public key to ~/.ssh/authorized_keys,
and allow the first "control Music" Automation prompt.

SSH (key, known_hosts, ControlMaster) is the shared helper in utilities/ssh_remote.py,
so the same key also serves the System Stats plugin.
"""

import base64
import logging
import subprocess

from utilities import ssh_remote
from utilities.paths import DESKTOP

log = logging.getLogger(__name__)

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
    """This Pi's public SSH key (shared with other plugins — see utilities/ssh_remote)."""
    return ssh_remote.ensure_key()


def _local(cmd: str, stdin: str, timeout: float) -> str:
    """Mac desktop app: the Music app is on this machine, so skip SSH."""
    try:
        r = subprocess.run(["sh", "-c", cmd], input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MacMusicError('Music did not answer — if macOS is showing an "allow … to control Music" '
                            'prompt, click Allow')
    if r.returncode != 0:
        if "-1743" in r.stderr or "Not authorized" in r.stderr:
            raise MacMusicError("macOS blocked control of Music — allow Flight Clock in System Settings → "
                                "Privacy & Security → Automation")
        raise MacMusicError(r.stderr.strip() or f"osascript failed ({r.returncode})")
    return r.stdout


def _ssh(host: str, user: str, remote_cmd: str, stdin: str, timeout: float = 8) -> str:
    if DESKTOP:
        return _local(remote_cmd, stdin, timeout)
    if not host or not user:
        raise MacMusicError("Set the Mac hostname and username")
    try:
        return ssh_remote.run(host, user, remote_cmd, stdin=stdin, timeout=timeout,
                              # A pending macOS "allow control of Music" prompt blocks osascript until timeout
                              timeout_hint='if the Mac is showing an "allow … to control Music" prompt, click Allow')
    except ssh_remote.SSHError as e:
        if "-1743" in e.stderr or "Not authorized" in e.stderr:
            raise MacMusicError("The Mac blocked control of Music — allow it in System Settings → "
                                "Privacy & Security → Automation")
        raise MacMusicError(str(e).replace("that machine", "the Mac"))


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
