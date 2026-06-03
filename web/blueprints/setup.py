"""
Setup blueprint — served only during first-boot WiFi setup mode.
Handles the full configuration wizard at /setup.
"""

import subprocess
import threading
import time
import os
import sys

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

setup_bp = Blueprint("setup", __name__)

# Written by setup_mode.run_setup_mode() so the wizard can signal "done"
_stop_event: threading.Event | None = None


def set_stop_event(event: threading.Event):
    global _stop_event
    _stop_event = event


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _cfg():
    return current_app.cfg


# ------------------------------------------------------------------
# Captive-portal intercept URLs
# Phones hit these to detect captive portals; we redirect to /setup.
# ------------------------------------------------------------------
_CAPTIVE_PATHS = [
    "/generate_204",
    "/hotspot-detect.html",
    "/ncsi.txt",
    "/connecttest.txt",
    "/redirect",
    "/success.txt",
    "/canonical.html",
]

for _path in _CAPTIVE_PATHS:
    def _make_redirect(p=_path):
        def _view():
            return redirect("/setup", code=302)
        _view.__name__ = f"captive_{p.strip('/').replace('.', '_').replace('-', '_')}"
        return _view
    setup_bp.add_url_rule(_path, view_func=_make_redirect())


# ------------------------------------------------------------------
# Main wizard page
# ------------------------------------------------------------------

@setup_bp.get("/")
def root():
    return redirect("/setup")


@setup_bp.get("/setup")
def setup_page():
    cfg = _cfg()
    location = cfg.get("location") or {}
    plugins_cfg = cfg.get("plugins") or {}
    plugin_order = cfg.get("plugin_order") or []
    secrets = cfg.get_all_secrets_masked() if hasattr(cfg, "get_all_secrets_masked") else {}

    # Build plugin list for the wizard
    known_plugins = [
        ("clock_weather",          "Clock & Weather"),
        ("flight_tracker",         "Flight Tracker (overhead)"),
        ("specific_flight_tracker","Specific Flight Tracker"),
        ("stock_ticker",           "Stock Ticker"),
        ("snow_report",            "Snow Report"),
        ("sports",                 "Sports Scores"),
        ("world_daylight",         "World Daylight Map"),
    ]
    plugin_list = []
    for pid, label in known_plugins:
        pcfg = plugins_cfg.get(pid, {})
        plugin_list.append({
            "id":       pid,
            "label":    label,
            "enabled":  pcfg.get("enabled", False),
            "duration": pcfg.get("display_duration", 30),
        })

    ft_cfg = plugins_cfg.get("flight_tracker", {})
    flights_cfg = cfg.get("flights") or {}

    return render_template(
        "setup.html",
        location=location,
        secrets=secrets,
        plugins=plugin_list,
        plugin_order=plugin_order,
        ft_min_altitude=ft_cfg.get("min_altitude", 8000),
        flights_email=flights_cfg.get("email", ""),
    )


# ------------------------------------------------------------------
# WiFi
# ------------------------------------------------------------------

@setup_bp.get("/setup/networks")
def setup_networks():
    try:
        subprocess.run(["sudo", "nmcli", "dev", "wifi", "rescan"],
                       capture_output=True, timeout=8)
        result = subprocess.run(
            ["sudo", "nmcli", "-e", "yes", "-t", "-f",
             "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        active = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        current_ssid = ""
        for line in active.stdout.splitlines():
            if line.startswith("yes:"):
                current_ssid = line[4:].replace("\\:", ":")
                break
        networks = []
        seen = set()
        for line in result.stdout.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) == 3:
                ssid, signal, security = parts[0].replace("\\:", ":"), parts[1], parts[2]
            elif len(parts) == 2:
                ssid, signal, security = parts[0].replace("\\:", ":"), parts[1], ""
            else:
                continue
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            try:
                sig_int = int(signal)
            except ValueError:
                sig_int = 0
            networks.append({
                "ssid": ssid, "signal": sig_int,
                "security": security, "current": ssid == current_ssid,
            })
        networks.sort(key=lambda x: x["signal"], reverse=True)
        return jsonify({"ok": True, "networks": networks, "current": current_ssid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "networks": [], "current": ""}), 500


@setup_bp.post("/setup/connect")
def setup_connect():
    data = request.get_json(force=True)
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return jsonify({"ok": False, "error": "SSID required"}), 400

    def _connect():
        cmd = ["sudo", "nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        subprocess.run(cmd, capture_output=True, timeout=30)

    threading.Thread(target=_connect, daemon=True).start()
    return jsonify({"ok": True})


@setup_bp.get("/setup/status")
def setup_status():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            capture_output=True, text=True, timeout=10,
        )
        connected = "connected" in result.stdout.lower()
        ssid = ""
        if connected:
            active = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                capture_output=True, text=True, timeout=5,
            )
            for line in active.stdout.splitlines():
                if line.startswith("yes:"):
                    ssid = line[4:].replace("\\:", ":")
                    break
        return jsonify({"ok": True, "connected": connected, "ssid": ssid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "connected": False, "ssid": ""}), 500


# ------------------------------------------------------------------
# Save full config from wizard
# ------------------------------------------------------------------

@setup_bp.post("/setup/save")
def setup_save():
    data = request.get_json(force=True)
    cfg = _cfg()

    # ---- location ----
    loc = data.get("location", {})
    try:
        lat = float(loc.get("lat", 0))
        lon = float(loc.get("lon", 0))
    except (TypeError, ValueError):
        lat, lon = 0.0, 0.0
    units = loc.get("units", "imperial")
    clock_format = loc.get("clock_format", "12hr")
    temp_loc = f"{lat},{lon}"

    cfg.save_config_section("location", "location_home", value=[lat, lon])
    cfg.save_config_section("location", "temperature_location", value=temp_loc)
    cfg.save_config_section("location", "units", value=units)
    cfg.save_config_section("location", "clock_format", value=clock_format)

    try:
        radius = int(loc.get("search_radius_nm", 30))
    except (TypeError, ValueError):
        radius = 30
    cfg.save_config_section("location", "search_radius_nm", value=radius)

    airport = loc.get("journey_code", "").strip().upper()
    cfg.save_config_section("location", "journey_code", value=airport)

    try:
        min_alt = int(loc.get("min_altitude", 8000))
    except (TypeError, ValueError):
        min_alt = 8000
    ft_cfg = dict(cfg.get_plugin_config("flight_tracker"))
    ft_cfg["min_altitude"] = min_alt
    cfg.save_plugin_config("flight_tracker", ft_cfg)

    flight_email = loc.get("flight_email", "").strip()
    cfg.save_config_section("flights", "email", value=flight_email)

    # ---- secrets / API keys ----
    secrets_data = data.get("secrets", {})
    if secrets_data:
        # Convert single-string api key fields to list format expected by config
        airlabs_key = secrets_data.get("airlabs_key", "").strip()
        fa_key = secrets_data.get("flightaware_key", "").strip()
        owm_key = secrets_data.get("owm_api_key", "").strip()
        new_secrets = {
            "tomorrow_api_key": secrets_data.get("tomorrow_api_key", "").strip(),
        }
        if airlabs_key:
            new_secrets["airlabs_api_keys"] = [airlabs_key]
        if fa_key:
            new_secrets["flightaware_api_keys"] = [fa_key]
        if owm_key:
            new_secrets["owm_api_key"] = owm_key
        cfg.save_secrets(new_secrets)

    # ---- plugins ----
    plugins_data = data.get("plugins", [])
    new_order = []
    for p in plugins_data:
        pid = p.get("id", "")
        if not pid:
            continue
        existing = dict(cfg.get("plugins", pid) or cfg.get_plugin_config(pid))
        existing["enabled"] = bool(p.get("enabled", False))
        try:
            existing["display_duration"] = int(p.get("duration", 30))
        except (TypeError, ValueError):
            pass
        cfg.save_plugin_config(pid, existing)
        if existing["enabled"]:
            new_order.append(pid)

    # Always ensure clock_weather is first and in the order
    if "clock_weather" not in new_order:
        new_order.insert(0, "clock_weather")
    cfg.save_config_section("plugin_order", value=new_order)

    return jsonify({"ok": True})


# ------------------------------------------------------------------
# Finish: save config and restart the app in normal mode
# ------------------------------------------------------------------

@setup_bp.post("/setup/finish")
def setup_finish():
    # Signal display loop to stop
    if _stop_event:
        _stop_event.set()

    # Restart the app via systemd (which will now have WiFi and skip setup mode)
    def _restart():
        time.sleep(0.8)
        subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"],
                       capture_output=True, timeout=10)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})
