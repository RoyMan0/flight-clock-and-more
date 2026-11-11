"""
Tips and feedback for the person who makes Flight Clock: the page footer, the About
page's first card (with QR codes in web/static/qr-*.svg) and the /feedback form.
"""

import logging
import platform
import socket
import threading
import time
from datetime import datetime

from utilities.paths import DESKTOP, DESKTOP_OS

log = logging.getLogger(__name__)

VENMO_HANDLE = "roy-hobbs"
VENMO_URL = f"https://venmo.com/u/{VENMO_HANDLE}"
PAYPAL_EMAIL = "rdhobbs@gmail.com"
PAYPAL_URL = "https://paypal.me/royman0"
FEEDBACK_EMAIL = "rdhobbs@gmail.com"
# The two projects link to each other (About page, READMEs)
PI_PROJECT_URL = "https://github.com/RoyMan0/flight-clock-and-more"
APP_PROJECT_URL = "https://github.com/RoyMan0/Flight-Clock-App/releases/latest"
APP_PUBLIC = False      # the app's repo is private for now: the Pi's About page says "coming soon"

KINDS = ("Issue", "Idea", "Question", "Other")
MAX_MESSAGE = 5000
MIN_GAP_SECS = 20                     # one message per device this often (a stuck button, not a spam filter)
_last_sent = 0.0
_lock = threading.Lock()


def template_vars() -> dict:
    return {"venmo_url": VENMO_URL, "venmo_handle": VENMO_HANDLE, "paypal_url": PAYPAL_URL,
            "paypal_email": PAYPAL_EMAIL, "feedback_email": FEEDBACK_EMAIL,
            "pi_project_url": PI_PROJECT_URL, "app_project_url": APP_PROJECT_URL, "app_public": APP_PUBLIC}


def device() -> str:
    if DESKTOP:
        return "Windows app" if DESKTOP_OS == "windows" else "Mac app"
    return "Raspberry Pi"


def send_feedback(kind: str, message: str, name: str = "", email: str = "") -> None:
    """Email the feedback to FEEDBACK_EMAIL. Raises ValueError (bad input) or RuntimeError (not sent)."""
    global _last_sent
    kind = kind if kind in KINDS else "Other"
    message = (message or "").strip()
    name, email = (name or "").strip()[:100], (email or "").strip()[:200]
    if len(message) < 3:
        raise ValueError("Write a message first")
    if len(message) > MAX_MESSAGE:
        raise ValueError(f"That's a long one: please keep it under {MAX_MESSAGE} characters")
    if email and ("@" not in email or " " in email or "\n" in email):
        raise ValueError("That email address doesn't look right")
    with _lock:
        if time.monotonic() - _last_sent < MIN_GAP_SECS:
            raise ValueError("Just sent one: wait a few seconds before sending another")
        _last_sent = time.monotonic()

    body = (f"{kind} from {name or 'someone'}{f' <{email}>' if email else ''}\n"
            f"{'-' * 60}\n{message}\n{'-' * 60}\n"
            f"Device: {device()} ({platform.system()} {platform.release()}, {platform.machine()})\n"
            f"Host: {socket.gethostname()}\n"
            f"Sent: {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}\n")
    subject = f"Flight Clock {kind.lower()}: {message.splitlines()[0][:60]}"

    from setup import email_alerts
    if not email_alerts._send(subject, body, receiver=FEEDBACK_EMAIL, reply_to=email or None):
        with _lock:
            _last_sent = 0.0                   # let them retry straight away
        raise RuntimeError("Couldn't send it from here (is this device online?)")
    log.info(f"[feedback] sent ({kind}, {len(message)} chars)")
