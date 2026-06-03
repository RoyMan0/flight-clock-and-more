"""
setup_mode.py — first-boot WiFi setup orchestration.

Captive portal approach:
  - A tiny Python UDP socket server listens on _DNS_PORT and returns
    HOTSPOT_IP for every DNS query (no dnsmasq, no NM dependency).
  - iptables redirects port 53 UDP → _DNS_PORT and port 80 TCP → 8080.
  - Works regardless of NM's DNS backend (systemd-resolved, dnsmasq, etc).
"""

import logging
import os
import socket
import subprocess
import struct
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

_DNS_PORT   = 5300   # our DNS spoof server (iptables redirects :53 here)
_SETUP_FLAG = "/tmp/flightclock-setup-requested"
_hotspot_connection_name = "Hotspot"


# ------------------------------------------------------------------
# WiFi state helpers
# ------------------------------------------------------------------

def is_wifi_client_connected() -> bool:
    """Return True only if connected in client (infra) mode, not AP/hotspot."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,MODE", "dev", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                if line[4:].strip().lower() == "infra":
                    return True
        return False
    except Exception:
        return False


def is_wifi_connected() -> bool:
    return is_wifi_client_connected()


def get_current_wifi_ssid() -> str:
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
# Python DNS spoof server
# ------------------------------------------------------------------

def _build_dns_response(data: bytes, ip_bytes: bytes) -> bytes | None:
    """
    Build a minimal DNS A response that returns ip_bytes for any query.
    Handles A queries; returns NXDOMAIN for anything else gracefully.
    """
    if len(data) < 12:
        return None
    txid    = data[:2]
    flags   = b'\x84\x00'          # QR=1, AA=1, RCODE=0
    qdcount = data[4:6]
    header  = txid + flags + qdcount + b'\x00\x01\x00\x00\x00\x00'
    question = data[12:]            # copy question section verbatim
    answer = (
        b'\xc0\x0c'                # name pointer to offset 12
        + b'\x00\x01'             # type A
        + b'\x00\x01'             # class IN
        + b'\x00\x00\x00\x00'    # TTL 0 (no caching)
        + b'\x00\x04'             # rdlength 4
        + ip_bytes
    )
    return header + question + answer


def run_dns_spoof(stop_event: threading.Event):
    """
    UDP server that listens on _DNS_PORT and returns HOTSPOT_IP for every
    DNS query.  iptables redirects port 53 here so clients never see the
    real internet — triggering the captive-portal popup.
    """
    ip_bytes = socket.inet_aton(HOTSPOT_IP)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", _DNS_PORT))
        sock.settimeout(1.0)
        log.info(f"[setup] DNS spoof server on port {_DNS_PORT}")
        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(512)
                response = _build_dns_response(data, ip_bytes)
                if response:
                    sock.sendto(response, addr)
            except socket.timeout:
                continue
            except Exception:
                continue
    except Exception as e:
        log.warning(f"[setup] DNS server error: {e}")
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ------------------------------------------------------------------
# Captive portal (iptables only — DNS handled by Python thread above)
# ------------------------------------------------------------------

def start_captive_portal() -> bool:
    """
    Add iptables rules:
      UDP :53  → _DNS_PORT  (our Python DNS spoof server)
      TCP :80  → 8080       (Flask setup wizard)
    """
    rules = [
        ["-p", "udp", "--dport", "53",
         "-j", "REDIRECT", "--to-port", str(_DNS_PORT)],
        ["-p", "tcp", "--dport", "80",
         "-j", "REDIRECT", "--to-port", "8080"],
    ]
    ok = True
    for rule in rules:
        try:
            subprocess.run(
                ["sudo", "iptables", "-t", "nat", "-A", "PREROUTING"] + rule,
                capture_output=True, timeout=5,
            )
        except Exception as e:
            log.warning(f"[setup] iptables add failed: {e}")
            ok = False
    if ok:
        log.info("[setup] Captive portal iptables rules active")
    return ok


def stop_captive_portal():
    log.info("[setup] Removing captive portal iptables rules")
    rules = [
        ["-p", "udp", "--dport", "53",
         "-j", "REDIRECT", "--to-port", str(_DNS_PORT)],
        ["-p", "tcp", "--dport", "80",
         "-j", "REDIRECT", "--to-port", "8080"],
    ]
    for rule in rules:
        try:
            subprocess.run(
                ["sudo", "iptables", "-t", "nat", "-D", "PREROUTING"] + rule,
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
    Full setup-mode flow.  Never returns normally — either killed by
    systemctl restart (from wizard) or os.execv with --skip-setup.
    """
    import os as _os
    import sys
    from core.display_manager import DisplayManager

    clear_setup_flag()

    original_ssid = get_current_wifi_ssid() if not args.no_hardware else ""

    if args.no_hardware:
        log.info("[setup] No-hardware mode — skipping network operations")
    else:
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

    # DNS spoof server (no-op in software mode — no real hotspot)
    if not args.no_hardware:
        dns_thread = threading.Thread(
            target=run_dns_spoof, args=(stop_display,),
            daemon=True, name="setup-dns",
        )
        dns_thread.start()

    def _display_loop():
        while not stop_display.is_set():
            qr_plugin.draw()
            display.swap()
            time.sleep(0.1)

    display_thread = threading.Thread(
        target=_display_loop, daemon=True, name="setup-display"
    )
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
