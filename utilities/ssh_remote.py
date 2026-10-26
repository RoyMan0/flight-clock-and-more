"""
Shared SSH helper — lets plugins run a command on another machine (the user's
Mac, a NAS, any Linux box) without installing anything there.

One key per Pi lives in data/ssh/ (gitignored). The machine authorises it once
by adding the public key to ~/.ssh/authorized_keys (the config page shows a
copy-ready command). Connections use BatchMode (never prompt), their own
known_hosts, and a ControlMaster socket so frequent polling stays cheap.

The key used to live in data/now_playing/; it is copied over on first use so
machines that already trust this Pi keep working.
"""

import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

_DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
SSH_DIR = os.path.join(_DATA, "ssh")
KEY_PATH = os.path.join(SSH_DIR, "id_ed25519")
_LEGACY_KEY = os.path.join(_DATA, "now_playing", "id_ed25519")


class SSHError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _migrate_legacy_key():
    if os.path.exists(KEY_PATH) or not os.path.exists(_LEGACY_KEY):
        return
    os.makedirs(SSH_DIR, exist_ok=True)
    for suffix in ("", ".pub"):
        if os.path.exists(_LEGACY_KEY + suffix):
            shutil.copy2(_LEGACY_KEY + suffix, KEY_PATH + suffix)
    os.chmod(KEY_PATH, 0o600)
    legacy_known = os.path.join(os.path.dirname(_LEGACY_KEY), "known_hosts")
    if os.path.exists(legacy_known) and not os.path.exists(os.path.join(SSH_DIR, "known_hosts")):
        shutil.copy2(legacy_known, os.path.join(SSH_DIR, "known_hosts"))
    log.info("[ssh] Moved this Pi's SSH key to data/ssh/")


def ensure_key() -> str:
    """Create this Pi's SSH key if needed; return the public key line."""
    _migrate_legacy_key()
    os.makedirs(SSH_DIR, exist_ok=True)
    if not os.path.exists(KEY_PATH):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "led-matrix",
                        "-f", KEY_PATH], check=True, capture_output=True, timeout=30)
    with open(KEY_PATH + ".pub") as f:
        return f.read().strip()


def authorize_command(key: str) -> str:
    """One-liner to paste into a terminal on the remote machine."""
    return f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '{key}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"


def run(host: str, user: str, remote_cmd: str, stdin: str = "", port: int = 22,
        timeout: float = 8, timeout_hint: str = "") -> str:
    """Run `remote_cmd` on user@host; return stdout. Raises SSHError with a friendly message."""
    host, user = (host or "").strip(), (user or "").strip()
    if not host or not user:
        raise SSHError("Set the hostname and username")
    _migrate_legacy_key()
    if not os.path.exists(KEY_PATH):
        raise SSHError("No SSH key yet — click Show key in the config page and add it to that machine")
    cmd = [
        "ssh", "-i", KEY_PATH, "-p", str(int(port or 22)),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={os.path.join(SSH_DIR, 'known_hosts')}",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={os.path.join(SSH_DIR, 'cm-%r@%h:%p')}",
        "-o", "ControlPersist=120",
        f"{user}@{host}", remote_cmd,
    ]
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SSHError(f"Timed out talking to {host}" + (f" — {timeout_hint}" if timeout_hint else ""))
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"ssh exited {r.returncode}"
        if "Permission denied" in msg:
            msg = "Permission denied — add this Pi's key to that machine (Show key)"
        elif "Could not resolve" in msg or "No route" in msg or "timed out" in msg.lower() \
                or "Connection refused" in msg:
            msg = f"Can't reach {host} — is it on, and is SSH / Remote Login enabled?"
        raise SSHError(msg, r.stderr or "")
    return r.stdout
