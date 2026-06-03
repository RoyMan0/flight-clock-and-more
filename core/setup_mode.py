"""
setup_mode.py — first-boot WiFi setup orchestration.

When the Pi has no WiFi connection on startup this module:
  1. Creates a WiFi hotspot via NetworkManager (nmcli)
  2. Starts a dnsmasq instance that redirects all DNS to the Pi (captive portal)
  3. Adds an iptables rule to forward port 80 → 8080 so phones' captive-portal
     detection auto-opens the setup wizard in the browser
  4. Runs the display in QR-code mode so users can scan to join the hotspot
  5. Waits (blocking) until NetworkManager reports a real WiFi client connection
  6. Tears everything down cleanly and reconnects original WiFi before returning

Callers: main.py, before DisplayManager drops root privileges.
"""

import logging
import os
import signal
import subprocess
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

_hotspot_connection_name = "Hotspot"


# ------------------------------------------------------------------
# WiFi state helpers
# ------------------------------------------------------------------

def is_wifi_client_connected() -> bool:
    """
    Return True only if connected to a WiFi network in client (infra) mode.
    Explicitly excludes hotspot/AP mode so the FltClk AP doesn't count.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,MODE", "dev", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                mode = line[4:].strip().lower()
                if mode == "infra":   # infrastructure = client mode
                    return True
        return False
    except Exception:
        return False


# Keep the old name as an alias so external callers aren't broken
def is_wifi_connected() -> bool:
    return is_wifi_client_connected()


def get_current_wifi_ssid() -> str:
    """Return the SSID of the currently active client WiFi connection, or ''."""
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
    """Tell NM to bring up a previously-known connection by name."""
    if not ssid:
        return
    log.info(f"[setup] Reconnecting to '{ssid}'…")
    try:
        subprocess.run(
            ["sudo", "nmcli", "con", "up", ssid],
            capture_output=True, timeout=20,
        )
    except Exception as e:
        log.warning(f"[setup] Reconnect failed: {e}")


def setup_requested() -> bool:
    """Return True if the flag file was written by the API endpoint."""
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
            ["sudo", "dnsmasq", "-C", _DNSMASQ_CONF,
             f"--pid-file={_DNSMASQ_PID}"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING",
             "-i", ap_interface, "-p", "tcp", "--dport", "80",
             "-j", "REDIRECT", "--to-port", "8080"],
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
            ["sudo", "iptables", "-t", "nat", "-D", "PREROUTING",
             "-i", ap_interface, "-p", "tcp", "--dport", "80",
             "-j", "REDIRECT", "--to-port", "8080"],
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
    """Block until a client WiFi connection is active or timeout/stop_event fires."""
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
        qr.make(fit=False)
        return qr.get_matrix()
    except Exception as e:
        log.warning(f"[setup] QR generation failed: {e}")
        return None


# ------------------------------------------------------------------
# Top-level orchestration
# ------------------------------------------------------------------

def run_setup_mode(cfg, args, forced: bool = False):
    """
    Full setup-mode flow. Called from main.py BEFORE DisplayManager drops
    root privileges.  Never returns normally.

    `forced` is True when setup was explicitly requested (--setup flag or API
    endpoint) rather than triggered by absent WiFi.  In forced mode we keep
    showing the wizard even if WiFi is already connected, and we reconnect to
    the original network on exit rather than waiting for a new connection.
    """
    import os
    import sys
    from core.display_manager import DisplayManager

    clear_setup_flag()

    ap_iface = "ap0"

    # Remember the original WiFi so we can reconnect after teardown
    original_ssid = get_current_wifi_ssid() if not args.no_hardware else ""

    if args.no_hardware:
        log.info("[setup] No-hardware mode — skipping hotspot and captive portal")
    else:
        hotspot_ok = start_hotspot()
        if not hotspot_ok:
            log.warning("[setup] Hotspot failed — continuing without AP")
        time.sleep(2)
        captive_ok = start_captive_portal(ap_iface)
        if not captive_ok:
            log.warning("[setup] Captive portal failed — users will need to type the IP")

    # Display init — drops root privileges here in hardware mode
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

    # ---- wait ----------------------------------------------------------
    if forced and not args.no_hardware:
        # Explicitly requested: stay in setup until user clicks Save & Start.
        # The /setup/finish endpoint will set stop_display OR restart via systemctl.
        log.info("[setup] Forced setup — waiting for wizard completion")
        stop_display.wait(timeout=600)
    else:
        log.info("[setup] Waiting for WiFi client connection…")
        wait_for_wifi_connected(stop_event=stop_display)

    # ---- teardown ------------------------------------------------------
    stop_display.set()
    display_thread.join(timeout=2)

    if not args.no_hardware:
        stop_captive_portal(ap_iface)
        stop_hotspot()
        # Give NM a moment then explicitly reconnect original network
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
    os.execv(sys.executable, [sys.executable] + new_argv)
