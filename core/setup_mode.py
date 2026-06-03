"""
setup_mode.py — first-boot WiFi setup orchestration.

When the Pi has no WiFi connection on startup this module:
  1. Writes a captive-portal DNS config into NM's dnsmasq-shared.d so that
     NM's own dnsmasq (started with the hotspot) returns HOTSPOT_IP for every
     DNS query — no separate dnsmasq process needed, no port conflicts.
  2. Creates a WiFi hotspot via NetworkManager (nmcli).
  3. Adds an iptables rule to forward port 80 → 8080.
  4. Runs the display in QR-code mode.
  5. Waits until a real WiFi client connection appears.
  6. Tears down cleanly and reconnects to the original WiFi.

Callers: main.py, before DisplayManager drops root privileges.
"""

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Hotspot constants
# ------------------------------------------------------------------
HOTSPOT_SSID     = "FltClk"
HOTSPOT_PASSWORD = "setup123"
HOTSPOT_IP       = "10.42.0.1"
HOTSPOT_IFACE    = "wlan0"

# NM reads extra dnsmasq config from this directory for shared connections.
# We write our captive-portal spoofing config here before starting the hotspot.
_NM_DNSMASQ_DIR  = "/etc/NetworkManager/dnsmasq-shared.d"
_NM_CAPTIVE_CONF = os.path.join(_NM_DNSMASQ_DIR, "flightclock-captive.conf")

_SETUP_FLAG = "/tmp/flightclock-setup-requested"
_hotspot_connection_name = "Hotspot"


# ------------------------------------------------------------------
# WiFi state helpers
# ------------------------------------------------------------------

def is_wifi_client_connected() -> bool:
    """
    Return True only if connected in client (infra) mode.
    Excludes the FltClk hotspot itself which NM also reports as 'connected'.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,MODE", "dev", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                mode = line[4:].strip().lower()
                if mode == "infra":
                    return True
        return False
    except Exception:
        return False


def is_wifi_connected() -> bool:
    return is_wifi_client_connected()


def get_current_wifi_ssid() -> str:
    """Return the SSID of the active client WiFi connection, or ''."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                return line[4:].replace("\\:", ":")
    except Exception:
        pass
    return ""


def reconnect_wifi(ssid: str):
    if not ssid:
        return
    log.info(f"[setup] Reconnecting to '{ssid}'…")
    try:
        subprocess.run(["sudo", "nmcli", "con", "up", ssid],
                       capture_output=True, timeout=20)
    except Exception as e:
        log.warning(f"[setup] Reconnect failed: {e}")


def setup_requested() -> bool:
    return os.path.exists(_SETUP_FLAG)


def clear_setup_flag():
    try:
        os.remove(_SETUP_FLAG)
    except FileNotFoundError:
        pass


def request_setup():
    with open(_SETUP_FLAG, "w") as f:
        f.write("")


# ------------------------------------------------------------------
# Captive portal
# ------------------------------------------------------------------

def start_captive_portal() -> bool:
    """
    Write the DNS spoof config into NM's dnsmasq-shared.d BEFORE the hotspot
    starts.  NM's own dnsmasq will pick it up automatically — no separate
    dnsmasq process, no port-53 conflict.
    Also adds an iptables rule: port 80 → 8080 by destination IP.
    """
    try:
        os.makedirs(_NM_DNSMASQ_DIR, exist_ok=True)
        with open(_NM_CAPTIVE_CONF, "w") as f:
            f.write(f"address=/#/{HOTSPOT_IP}\n")
        log.info(f"[setup] Captive portal DNS config written to {_NM_CAPTIVE_CONF}")
    except Exception as e:
        log.warning(f"[setup] Could not write dnsmasq config: {e}")

    try:
        subprocess.run(
            ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
             "-d", HOTSPOT_IP, "-p", "tcp", "--dport", "80",
             "-j", "REDIRECT", "--to-port", "8080"],
            capture_output=True, timeout=5,
        )
        log.info("[setup] iptables: port 80 → 8080 redirect active")
        return True
    except Exception as e:
        log.warning(f"[setup] iptables failed: {e}")
        return False


def stop_captive_portal():
    log.info("[setup] Stopping captive portal")
    try:
        os.remove(_NM_CAPTIVE_CONF)
    except FileNotFoundError:
        pass
    try:
        subprocess.run(
            ["sudo", "iptables", "-t", "nat", "-D", "PREROUTING",
             "-d", HOTSPOT_IP, "-p", "tcp", "--dport", "80",
             "-j", "REDIRECT", "--to-port", "8080"],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log.warning(f"[setup] iptables remove error: {e}")


# ------------------------------------------------------------------
# Hotspot
# ------------------------------------------------------------------

def start_hotspot() -> bool:
    log.info(f"[setup] Starting hotspot SSID={HOTSPOT_SSID}")
    try:
        result = subprocess.run(
            ["sudo", "nmcli", "device", "wifi", "hotspot",
             "ssid", HOTSPOT_SSID,
             "password", HOTSPOT_PASSWORD,
             "ifname", HOTSPOT_IFACE,
             "con-name", _hotspot_connection_name],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            log.warning(f"[setup] nmcli hotspot failed: {result.stderr.strip()}")
            return False
        log.info(f"[setup] Hotspot active at {HOTSPOT_IP}")
        return True
    except Exception as e:
        log.warning(f"[setup] Hotspot start error: {e}")
        return False


def stop_hotspot():
    log.info("[setup] Stopping hotspot")
    try:
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", _hotspot_connection_name],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        log.warning(f"[setup] Hotspot stop error: {e}")


# ------------------------------------------------------------------
# Wait for connection
# ------------------------------------------------------------------

def wait_for_wifi_connected(
    poll_interval: float = 4.0,
    timeout: float = 600.0,
    stop_event: threading.Event | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            return False
        if is_wifi_client_connected():
            return True
        time.sleep(poll_interval)
    return False


# ------------------------------------------------------------------
# QR matrix helper
# ------------------------------------------------------------------

def generate_wifi_qr_matrix(ssid: str, password: str) -> list[list[bool]] | None:
    """
    Return a 2-D boolean matrix for a WIFI: QR code.
    border=0 keeps the matrix tight so it fits the 64×32 display.
    """
    try:
        import qrcode
        import qrcode.constants
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=0,
        )
        qr.add_data(f"WIFI:T:WPA;S:{ssid};P:{password};;")
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        log.info(f"[setup] QR code: version={qr.version}, size={size}×{size}px")
        if size > 32:
            log.warning(f"[setup] QR too large ({size}px > 32px) — text fallback only")
            return None
        return matrix
    except Exception as e:
        log.warning(f"[setup] QR generation failed: {e}")
        return None


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------

def run_setup_mode(cfg, args, forced: bool = False):
    """
    Full setup-mode flow. Never returns normally.

    `forced` = True when explicitly requested (--setup flag or API endpoint):
    keeps the wizard open even if WiFi is already connected, and waits for
    the user to click Save & Start.
    """
    import os as _os
    import sys
    from core.display_manager import DisplayManager

    clear_setup_flag()

    original_ssid = get_current_wifi_ssid() if not args.no_hardware else ""

    if args.no_hardware:
        log.info("[setup] No-hardware mode — skipping network operations")
    else:
        # Write captive portal config BEFORE starting hotspot so NM picks it up
        start_captive_portal()
        hotspot_ok = start_hotspot()
        if not hotspot_ok:
            log.warning("[setup] Hotspot failed — continuing without AP")

    display_cfg = cfg.get("display") or {}
    display = DisplayManager(display_cfg, software_mode=args.no_hardware)
    log.info(f"[setup] Display initialised ({'software' if args.no_hardware else 'hardware'} mode)")

    from plugins.setup_qr.manager import SetupQRPlugin
    qr_plugin = SetupQRPlugin(display, {}, {})

    stop_display = threading.Event()

    def _display_loop():
        while not stop_display.is_set():
            qr_plugin.draw()
            display.swap()
            time.sleep(0.1)

    display_thread = threading.Thread(target=_display_loop, daemon=True, name="setup-display")
    display_thread.start()

    from web.app import create_app
    from web.blueprints.setup import set_stop_event
    set_stop_event(stop_display)

    app = create_app(cfg, display, None, setup_mode=True)
    web_cfg = cfg.get("web") or {}
    web_thread = threading.Thread(
        target=lambda: app.run(
            host=web_cfg.get("host", "0.0.0.0"),
            port=web_cfg.get("port", 8080),
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="setup-web",
    )
    web_thread.start()
    log.info(f"[setup] Setup wizard at http://{HOTSPOT_IP}:8080/setup")

    if forced and not args.no_hardware:
        log.info("[setup] Forced setup — waiting for wizard completion")
        stop_display.wait(timeout=600)
    else:
        log.info("[setup] Waiting for WiFi client connection…")
        wait_for_wifi_connected(stop_event=stop_display)

    stop_display.set()
    display_thread.join(timeout=2)

    if not args.no_hardware:
        stop_captive_portal()
        stop_hotspot()
        time.sleep(2)
        reconnect_wifi(original_ssid)

    display.clear()

    if args.no_hardware:
        log.info("[setup] Setup mode complete (no-hardware) — exiting")
        sys.exit(0)

    new_argv = [a for a in sys.argv if a not in ("--setup",)]
    if "--skip-setup" not in new_argv:
        new_argv.append("--skip-setup")
    log.info(f"[setup] Restarting: {sys.executable} {' '.join(new_argv)}")
    _os.execv(sys.executable, [sys.executable] + new_argv)
