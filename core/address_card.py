"""
The clock's web address on the panel for a few seconds at startup, so people can
find its settings page on their home network (http://<IP>:8080). Longest right
after the first-boot setup, when the person has just moved their phone back to
their home Wi-Fi and is looking for it.

Pi only (main.py); the desktop app has its own windows.
"""

import logging
import os
import socket
import subprocess
import time

from PIL import Image

from utilities.paths import DATA_ROOT

log = logging.getLogger(__name__)

W, H = 64, 32
GREY, WHITE, YELLOW, CYAN = (150, 150, 160), (235, 235, 235), (255, 200, 60), (80, 200, 255)

# The setup wizard leaves this behind when it finishes; the next start shows the card longer
JUST_SET_UP = os.path.join(DATA_ROOT, "data", "just_set_up")
BOOT_SECONDS, AFTER_SETUP_SECONDS = 20, 60


def mark_just_set_up():
    try:
        os.makedirs(os.path.dirname(JUST_SET_UP), exist_ok=True)
        open(JUST_SET_UP, "w").close()
    except OSError as e:
        log.debug(f"[address] couldn't mark setup: {e}")


def startup_seconds(on_boot: bool = True) -> int:
    """How long to show the card at this start (and clear the setup marker). on_boot is the
    display.show_address_at_startup switch: off skips ordinary restarts, but the card still
    shows after the setup wizard, when the person needs the address."""
    if os.path.exists(JUST_SET_UP):
        try:
            os.remove(JUST_SET_UP)
        except OSError:
            pass
        return AFTER_SETUP_SECONDS
    return BOOT_SECONDS if on_boot else 0


def local_ip() -> str:
    """This machine's address on the local network, or "" (no network yet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))          # picks the outgoing interface; sends nothing
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith(("127.", "0.")):
            return ip
    except OSError:
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout.split()
        return next((a for a in out if "." in a and not a.startswith("127.")), "")
    except Exception:
        return ""


def render(ip: str, port: int) -> Image.Image:
    """The card: what it is, the IP, the port, and that it's on the home network."""
    from plugins.transit.render import _FONT, _bdf_draw, _bdf_width

    img = Image.new("RGB", (W, H), (0, 0, 0))

    def centre(y, text, colour):
        _bdf_draw(img, max(0, (W - _bdf_width(text, _FONT)) // 2), y, text, _FONT, colour)

    if not ip:
        centre(6, "Joining", GREY)
        centre(14, "Wi-Fi...", WHITE)
        return img
    centre(0, "Settings at", GREY)
    centre(8, ip, YELLOW)
    centre(16, f"port {port}", CYAN)
    centre(25, "on your Wi-Fi", GREY)      # the page is only reachable on the home network
    return img


def show(display, seconds: float, port: int = 8080, wait_for_network: float = 30.0):
    """Show the card for `seconds` once the clock has an address. While it's still joining
    Wi-Fi (just after setup), shows "Joining Wi-Fi..." for up to `wait_for_network` seconds;
    with no address by then it gives up quietly. Never raises: the clock must start anyway."""
    if seconds <= 0:
        return
    try:
        from plugins.transit.render import push_pil_to_canvas
        deadline = time.monotonic() + wait_for_network
        ip = local_ip()
        while not ip and time.monotonic() < deadline:
            push_pil_to_canvas(render("", port), display)
            display.swap()
            time.sleep(1)
            ip = local_ip()
        if not ip:
            log.info("[address] No network address yet; skipping the address card")
            return
        log.info(f"[address] Settings page: http://{ip}:{port}")
        frame = render(ip, port)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            push_pil_to_canvas(frame, display)
            display.swap()
            time.sleep(0.5)
        display.clear()
    except Exception as e:
        log.warning(f"[address] Couldn't show the address card: {e}")
