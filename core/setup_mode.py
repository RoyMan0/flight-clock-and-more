"""
setup_mode.py — first-boot WiFi setup orchestration.

When the Pi has no WiFi connection on startup this module:
  1. Creates a WiFi hotspot via NetworkManager (nmcli)
  2. Starts a dnsmasq instance that redirects all DNS to the Pi (captive portal)
  3. Adds an iptables rule to forward port 80 → 8080 so phones' captive-portal
     detection auto-opens the setup wizard in the browser
  4. Runs the display in QR-code mode so users can scan to join the hotspot
  5. Waits (blocking) until NetworkManager reports a real WiFi connection
  6. Tears everything down cleanly before returning

Callers: main.py, before DisplayManager drops root privileges.
"""

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Hotspot constants — kept short so the WIFI: QR string fits version 1
# ------------------------------------------------------------------
HOTSPOT_SSID     = "FltClk"
HOTSPOT_PASSWORD = "setup1234"
HOTSPOT_IP       = "10.42.0.1"
HOTSPOT_IFACE    = "wlan0"

_DNSMASQ_CONF = "/tmp/flightclock-dnsmasq.conf"
_DNSMASQ_PID  = "/tmp/flightclock-dnsmasq.pid"
_SETUP_FLAG   = "/tmp/flightclock-setup-requested"

_hotspot_connection_name = "Hotspot"   # NM connection name assigned by nmcli


# ------------------------------------------------------------------
# WiFi state helpers
# ------------------------------------------------------------------

def is_wifi_connected() -> bool:
    """Return True if NetworkManager reports any active internet connection."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            capture_output=True, text=True, timeout=10,
        )
        return "connected" in result.stdout.lower()
    except Exception:
        return False


def setup_requested() -> bool:
    """Return True if a flag file was written by the API endpoint."""
    return os.path.exists(_SETUP_FLAG)


def clear_setup_flag():
    try:
        os.remove(_SETUP_FLAG)
    except FileNotFoundError:
        pass


def request_setup():
    """Write the flag file so the next startup enters setup mode."""
    with open(_SETUP_FLAG, "w") as f:
        f.write("")


# ------------------------------------------------------------------
# Hotspot management
# ------------------------------------------------------------------

def start_hotspot() -> bool:
    """Create a WiFi access point using NetworkManager. Returns True on success."""
    log.info(f"[setup] Starting hotspot SSID={HOTSPOT_SSID}")
    try:
        result = subprocess.run(
            [
                "sudo", "nmcli", "device", "wifi", "hotspot",
                "ssid", HOTSPOT_SSID,
                "password", HOTSPOT_PASSWORD,
                "ifname", HOTSPOT_IFACE,
                "con-name", _hotspot_connection_name,
            ],
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
# Captive portal (dnsmasq + iptables)
# ------------------------------------------------------------------

def start_captive_portal(ap_interface: str = "ap0") -> bool:
    """
    Write a dnsmasq config that spoofs all DNS to HOTSPOT_IP, then add an
    iptables rule to forward port 80 → 8080 so phone browsers hit Flask.
    Returns True if both succeeded.
    """
    conf = (
        f"interface={ap_interface}\n"
        "bind-interfaces\n"
        f"dhcp-range={HOTSPOT_IP[:-1]}100,{HOTSPOT_IP[:-1]}200,1h\n"
        f"address=/#/{HOTSPOT_IP}\n"
        "no-resolv\n"
        "no-hosts\n"
    )
    try:
        with open(_DNSMASQ_CONF, "w") as f:
            f.write(conf)

        subprocess.run(
            ["sudo", "dnsmasq",
             "-C", _DNSMASQ_CONF,
             f"--pid-file={_DNSMASQ_PID}"],
            capture_output=True, timeout=10,
        )

        subprocess.run(
            [
                "sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
                "-i", ap_interface, "-p", "tcp", "--dport", "80",
                "-j", "REDIRECT", "--to-port", "8080",
            ],
            capture_output=True, timeout=5,
        )
        log.info("[setup] Captive portal active (dnsmasq + iptables)")
        return True
    except Exception as e:
        log.warning(f"[setup] Captive portal start error: {e}")
        return False


def stop_captive_portal(ap_interface: str = "ap0"):
    log.info("[setup] Stopping captive portal")
    try:
        if os.path.exists(_DNSMASQ_PID):
            with open(_DNSMASQ_PID) as f:
                pid = int(f.read().strip())
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.remove(_DNSMASQ_PID)
            except FileNotFoundError:
                pass
    except Exception as e:
        log.warning(f"[setup] dnsmasq stop error: {e}")

    try:
        subprocess.run(
            [
                "sudo", "iptables", "-t", "nat", "-D", "PREROUTING",
                "-i", ap_interface, "-p", "tcp", "--dport", "80",
                "-j", "REDIRECT", "--to-port", "8080",
            ],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log.warning(f"[setup] iptables remove error: {e}")

    try:
        os.remove(_DNSMASQ_CONF)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------
# Wait for connection
# ------------------------------------------------------------------

def wait_for_wifi_connected(
    poll_interval: float = 4.0,
    timeout: float = 600.0,
    stop_event: threading.Event | None = None,
) -> bool:
    """
    Block until NM reports a real WiFi connection or timeout expires.
    Returns True if connected, False if timed out.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop_event and stop_event.is_set():
            return False
        if is_wifi_connected():
            return True
        time.sleep(poll_interval)
    return False


# ------------------------------------------------------------------
# QR matrix helper
# ------------------------------------------------------------------

def generate_wifi_qr_matrix(ssid: str, password: str) -> list[list[bool]] | None:
    """
    Return a 2-D boolean matrix (True = dark module) for a WIFI: QR code.
    Returns None if the qrcode library is unavailable or content overflows.
    """
    try:
        import qrcode
        import qrcode.constants
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(f"WIFI:T:WPA;S:{ssid};P:{password};;")
        qr.make(fit=False)   # raises DataOverflowError if too large for version 1
        return qr.get_matrix()
    except Exception as e:
        log.warning(f"[setup] QR generation failed: {e}")
        return None


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------

def run_setup_mode(cfg, args):
    """
    Full setup-mode flow.  Called from main.py BEFORE DisplayManager drops
    root privileges.

    This function never returns normally — it either:
    - Is killed by systemctl restart (triggered from the setup wizard "Save & Start")
    - Calls os.execv to restart the process with --skip-setup once WiFi is detected
    """
    import os
    import sys
    from core.display_manager import DisplayManager

    clear_setup_flag()

    ap_iface = "ap0"
    if args.no_hardware:
        # Development mode: skip OS-level network operations
        log.info("[setup] No-hardware mode — skipping hotspot and captive portal")
    else:
        # ---- start hotspot (needs root) ---------------------------------
        hotspot_ok = start_hotspot()
        if not hotspot_ok:
            log.warning("[setup] Hotspot failed — setup mode continuing without AP")

        # NM may rename the AP interface; try ap0 first
        time.sleep(2)  # give NM a moment to bring the AP interface up
        captive_ok = start_captive_portal(ap_iface)
        if not captive_ok:
            log.warning("[setup] Captive portal failed — users will need to type the IP")

    # ---- init display (drops root privileges here in hardware mode) -
    display_cfg = cfg.get("display") or {}
    display = DisplayManager(display_cfg, software_mode=args.no_hardware)
    log.info(f"[setup] Display initialised ({'software' if args.no_hardware else 'hardware'} mode)")

    # ---- show QR plugin on display ----------------------------------
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

    # ---- start Flask in setup mode ----------------------------------
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
    log.info(f"[setup] Setup wizard running at http://{HOTSPOT_IP}:8080/setup")

    # ---- wait for WiFi connection ------------------------------------
    log.info("[setup] Waiting for WiFi connection…")
    connected = wait_for_wifi_connected(stop_event=stop_display)
    if connected:
        log.info("[setup] WiFi connected — restarting in normal mode")
    else:
        log.warning("[setup] Setup timed out — restarting in normal mode")

    stop_display.set()
    display_thread.join(timeout=2)

    # ---- teardown ---------------------------------------------------
    if not args.no_hardware:
        stop_captive_portal(ap_iface)
        stop_hotspot()
    display.clear()

    if args.no_hardware:
        # Development mode: just exit cleanly; caller re-runs manually
        log.info("[setup] Setup mode complete (no-hardware) — exiting")
        sys.exit(0)

    # ---- restart the process cleanly in normal mode -----------------
    # This avoids the dual-Flask conflict when main() tries to start its
    # own web server on the same port after we return.
    new_argv = [a for a in sys.argv if a not in ("--setup",)]
    if "--skip-setup" not in new_argv:
        new_argv.append("--skip-setup")
    log.info(f"[setup] Restarting: {sys.executable} {' '.join(new_argv)}")
    os.execv(sys.executable, [sys.executable] + new_argv)
