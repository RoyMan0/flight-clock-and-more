"""
REST API blueprint — all endpoints under /api/
"""

import base64
import io
import json
import os
import time
import subprocess
import threading
from flask import Blueprint, current_app, jsonify, request, Response, stream_with_context

api_bp = Blueprint("api", __name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CLOSEST_FILE  = os.path.join(BASE_DIR, "close.txt")
FARTHEST_FILE = os.path.join(BASE_DIR, "farthest.txt")
HISTORY_FILE  = os.path.join(BASE_DIR, "data", "flight_history.json")


def _cfg():
    return current_app.cfg

def _pm():
    return current_app.plugin_manager

def _display():
    return current_app.display


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------

@api_bp.get("/status")
def status():
    pm = _pm()
    status_data = pm.get_status() if pm else {}
    return jsonify(status_data)


@api_bp.get("/system")
def system_info():
    info = {
        "uptime": _uptime(),
        "cpu_temp": _cpu_temp(),
        "memory": _memory(),
        "hostname": _hostname(),
    }
    return jsonify(info)


# ------------------------------------------------------------------
# Display snapshot (for live preview)
# ------------------------------------------------------------------

@api_bp.get("/display/snapshot")
def display_snapshot():
    d = _display()
    if d is None:
        return jsonify({"error": "display not available"}), 503
    try:
        img = d.get_snapshot()
        # Scale up 4× for visibility in the browser
        img = img.resize((img.width * 4, img.height * 4), resample=0)  # NEAREST
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return jsonify({"image": f"data:image/png;base64,{b64}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# SSE stream (live dashboard updates every 500ms)
# ------------------------------------------------------------------

@api_bp.get("/stream/status")
def stream_status():
    def generate():
        while True:
            pm = _pm()
            d = _display()
            status_data = pm.get_status() if pm else {}

            # Snapshot image
            img_data = ""
            if d:
                try:
                    img = d.get_snapshot()
                    img = img.resize((img.width * 4, img.height * 4), resample=0)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    img_data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
                except Exception:
                    pass

            payload = {
                **status_data,
                "snapshot": img_data,
                "sys": {
                    "cpu_temp": _cpu_temp(),
                    "memory": _memory(),
                    "uptime": _uptime(),
                },
            }
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------
# Plugin management
# ------------------------------------------------------------------

@api_bp.get("/plugins")
def list_plugins():
    pm = _pm()
    if pm is None:
        return jsonify([])
    cfg = _cfg()
    plugins = []
    for pid, plugin in pm._plugins.items():
        plugins.append({
            "id": pid,
            "enabled": plugin.enabled,
            "config": cfg.get_plugin_config(pid) if cfg else {},
            "has_live": plugin.has_live_content(),
        })
    return jsonify(plugins)


@api_bp.get("/plugins/<plugin_id>/config")
def get_plugin_config(plugin_id):
    cfg = _cfg()
    if cfg is None:
        return jsonify({}), 503
    return jsonify(cfg.get_plugin_config(plugin_id))


@api_bp.post("/plugins/<plugin_id>/config")
def save_plugin_config(plugin_id):
    cfg = _cfg()
    pm = _pm()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    data = request.get_json(force=True)
    cfg.save_plugin_config(plugin_id, data)
    if pm:
        pm.reload_rotation()
    return jsonify({"ok": True})


@api_bp.post("/plugins/order")
def save_plugin_order():
    cfg = _cfg()
    pm = _pm()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    order = request.get_json(force=True)
    if not isinstance(order, list):
        return jsonify({"error": "expected list"}), 400
    cfg.save_config_section("plugin_order", value=order)
    if pm:
        pm.reload_rotation()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# On-demand display control
# ------------------------------------------------------------------

@api_bp.post("/display/show/<plugin_id>")
def force_show(plugin_id):
    pm = _pm()
    if pm is None:
        return jsonify({"error": "plugin manager not available"}), 503
    ok = pm.force_plugin(plugin_id)
    return jsonify({"ok": ok})


@api_bp.post("/display/return")
def return_to_rotation():
    pm = _pm()
    if pm is None:
        return jsonify({"error": "plugin manager not available"}), 503
    pm.reload_rotation()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# System configuration
# ------------------------------------------------------------------

@api_bp.get("/config/system")
def get_system_config():
    cfg = _cfg()
    if cfg is None:
        return jsonify({}), 503
    return jsonify({
        "display": cfg.get("display") or {},
        "location": cfg.get("location") or {},
        "flights": cfg.get("flights") or {},
        "web": cfg.get("web") or {},
    })


@api_bp.post("/config/system")
def save_system_config():
    cfg = _cfg()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    data = request.get_json(force=True)
    full = cfg.get_all()
    for key in ("display", "location", "flights", "web"):
        if key in data:
            full[key] = data[key]
    cfg.save_config(full)
    return jsonify({"ok": True})


@api_bp.get("/config/secrets")
def get_secrets():
    cfg = _cfg()
    if cfg is None:
        return jsonify({}), 503
    return jsonify(cfg.get_all_secrets_masked())


@api_bp.post("/config/secrets")
def save_secrets():
    cfg = _cfg()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    data = request.get_json(force=True)
    cfg.save_secrets(data)
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# API usage metrics
# ------------------------------------------------------------------

@api_bp.get("/metrics")
def get_metrics():
    from utilities.overhead import get_al_metrics, get_fa_metrics, _get_keys, _get_reset_days
    from utilities.rain import get_owm_metrics
    cfg = _cfg()
    if cfg is None:
        return jsonify({}), 503
    secrets = cfg.get_all_secrets()
    budget = float(secrets.get("flightaware_monthly_budget", 4.50))
    al_keys = _get_keys(secrets, "airlabs_api_keys", "airlabs_api_key")
    fa_keys = _get_keys(secrets, "flightaware_api_keys", "flightaware_api_key")
    al_reset_days = _get_reset_days(secrets, "airlabs_reset_days", len(al_keys))
    fa_reset_days = _get_reset_days(secrets, "flightaware_reset_days", len(fa_keys))
    return jsonify({
        "airlabs":     get_al_metrics(al_keys, al_reset_days),
        "flightaware": get_fa_metrics(fa_keys, budget, fa_reset_days),
        "owm":         get_owm_metrics(),
    })


# ------------------------------------------------------------------
# Backup / restore
# ------------------------------------------------------------------

@api_bp.get("/config/backups")
def list_backups():
    cfg = _cfg()
    if cfg is None:
        return jsonify([]), 503
    return jsonify(cfg.list_backups())


@api_bp.post("/config/backup")
def create_backup():
    cfg = _cfg()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    path = cfg.create_backup()
    return jsonify({"ok": True, "path": path})


@api_bp.post("/config/restore/<filename>")
def restore_backup(filename):
    cfg = _cfg()
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    ok = cfg.restore_backup(filename)
    return jsonify({"ok": ok})


# ------------------------------------------------------------------
# Sports team browser
# ------------------------------------------------------------------

@api_bp.get("/sports/teams")
def sports_teams():
    import requests as _req

    ESPN  = "https://site.api.espn.com/apis/site/v2/sports"
    ESPNW = "https://site.web.api.espn.com/apis/site/v2/sports"
    # (league_label, url_base, paginate)
    SOURCES = [
        ("NFL",       f"{ESPN}/football/nfl/teams",                          False),
        ("NBA",       f"{ESPN}/basketball/nba/teams",                        False),
        ("MLB",       f"{ESPN}/baseball/mlb/teams",                          False),
        ("NHL",       f"{ESPNW}/hockey/nhl/teams",                           False),
        ("NCAAF",     f"{ESPN}/football/college-football/teams",              True),
        ("NCAAB",     f"{ESPN}/basketball/mens-college-basketball/teams",     True),
        ("MLS",          f"{ESPN}/soccer/usa.1/teams",                          False),
        ("EPL",          f"{ESPN}/soccer/eng.1/teams",                          False),
        ("Championship", f"{ESPN}/soccer/eng.2/teams",                          False),
        ("Bundesliga",   f"{ESPN}/soccer/ger.1/teams",                          False),
        ("UCL",          f"{ESPN}/soccer/uefa.champions/teams",                 False),
        ("La Liga",      f"{ESPN}/soccer/esp.1/teams",                          False),
        ("World Cup",    f"{ESPNW}/soccer/fifa.world/teams",                    False),
    ]
    PAGE_SIZE = 200

    def _parse_page(data, seen):
        teams = []
        for sport in data.get("sports", []):
            for lg in sport.get("leagues", []):
                for entry in lg.get("teams", []):
                    t = entry.get("team", entry)
                    abbrev = t.get("abbreviation", "")
                    name = t.get("displayName", "")
                    if abbrev and name and abbrev not in seen:
                        teams.append({"abbrev": abbrev, "name": name})
                        seen.add(abbrev)
        for entry in data.get("teams", []):
            t = entry.get("team", entry)
            abbrev = t.get("abbreviation", "")
            name = t.get("displayName", "")
            if abbrev and name and abbrev not in seen:
                teams.append({"abbrev": abbrev, "name": name})
                seen.add(abbrev)
        return teams

    def _fetch(url_base, paginate):
        seen = set()
        all_teams = []
        hdrs = {"User-Agent": "LEDMatrix/1.0"}
        if not paginate:
            r = _req.get(url_base, timeout=15, headers=hdrs)
            all_teams = _parse_page(r.json(), seen)
        else:
            page = 1
            while True:
                url = f"{url_base}?limit={PAGE_SIZE}&page={page}"
                r = _req.get(url, timeout=15, headers=hdrs)
                batch = _parse_page(r.json(), seen)
                all_teams.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
                page += 1
        return sorted(all_teams, key=lambda t: t["name"])

    result = {}
    for league, url, paginate in SOURCES:
        try:
            result[league] = _fetch(url, paginate)
        except Exception:
            result[league] = []
    return jsonify(result)


# ------------------------------------------------------------------
# Flight data (for dashboard panel)
# ------------------------------------------------------------------

@api_bp.get("/flights/closest")
def closest():
    return jsonify(_load_json(CLOSEST_FILE, {}))


@api_bp.get("/flights/farthest")
def farthest():
    return jsonify(_load_json(FARTHEST_FILE, []))


@api_bp.get("/flights/current")
def current_flights():
    pm = _pm()
    if pm is None:
        return jsonify([])
    ft = pm._plugins.get("flight_tracker")
    if ft is None or not hasattr(ft, "get_current_flights"):
        return jsonify([])
    return jsonify(ft.get_current_flights())


@api_bp.get("/flights/history")
def flight_history():
    return jsonify(_load_json(HISTORY_FILE, {}))


@api_bp.get("/flights/history/summary")
def flight_history_summary():
    history = _load_json(HISTORY_FILE, {})
    if not isinstance(history, dict):
        history = {}

    cfg = _cfg()
    home_airport = ""
    if cfg:
        home_airport = (cfg.get("location") or {}).get("journey_code", "")

    days = sorted(history.keys())
    num_days = len(days)
    if num_days == 0:
        return jsonify({
            "daily_average": 0, "busiest_day_date": None, "busiest_day_count": 0,
            "peak_hour": None, "peak_hour_avg": 0, "peak_dow": None, "peak_dow_avg": 0,
            "days_tracked": 0, "hourly_avg": [0]*24, "dow_avg": [0]*7,
            "airline_breakdown": {}, "local_vs_flyover": {"local": 0, "flyover": 0, "unknown": 0},
            "daily_log": [],
        })

    # Accumulators
    hourly_totals = [0] * 24
    dow_totals    = [0] * 7   # 0=Mon … 6=Sun in isoweekday; we convert to Sun=0
    dow_counts    = [0] * 7
    airline_counts  = {}
    ac_type_counts  = {}
    local = flyover = unknown = 0
    busiest_date = None
    busiest_count = 0
    total_flights = 0
    daily_log = []

    for date_str in days:
        day = history[date_str]
        if not isinstance(day, dict):
            continue
        count = day.get("count", 0)
        total_flights += count
        if count > busiest_count:
            busiest_count = count
            busiest_date  = date_str

        # day-of-week: Python weekday() Mon=0..Sun=6; convert to Sun=0..Sat=6
        try:
            from datetime import date as _date
            d = _date.fromisoformat(date_str)
            dow = (d.weekday() + 1) % 7   # Mon=1..Sat=6, Sun=0
            dow_totals[dow] += count
            dow_counts[dow] += 1
        except ValueError:
            pass

        for flight in day.get("flights", []):
            hour = flight.get("hour")
            if isinstance(hour, int) and 0 <= hour <= 23:
                hourly_totals[hour] += 1

            airline = (flight.get("airline_icao") or "").strip()
            if airline:
                airline_counts[airline] = airline_counts.get(airline, 0) + 1
            else:
                airline_counts["Unknown"] = airline_counts.get("Unknown", 0) + 1

            ac_type = (flight.get("aircraft_type") or "").strip()
            if ac_type:
                ac_type_counts[ac_type] = ac_type_counts.get(ac_type, 0) + 1
            else:
                ac_type_counts["Unknown"] = ac_type_counts.get("Unknown", 0) + 1

            orig = (flight.get("origin") or "").strip()
            dest = (flight.get("destination") or "").strip()
            if not orig and not dest:
                unknown += 1
            elif home_airport and (orig == home_airport or dest == home_airport):
                local += 1
            else:
                flyover += 1

        daily_log.append({
            "date":                date_str,
            "count":               count,
            "first_seen":          day.get("first_seen"),
            "first_seen_callsign": day.get("first_seen_callsign"),
            "last_seen":           day.get("last_seen"),
            "last_seen_callsign":  day.get("last_seen_callsign"),
        })

    daily_log.sort(key=lambda x: x["date"], reverse=True)

    daily_avg = round(total_flights / num_days, 1) if num_days else 0

    hourly_avg = [round(t / num_days, 1) for t in hourly_totals]
    peak_hour  = hourly_avg.index(max(hourly_avg))

    dow_avg = [round(dow_totals[i] / dow_counts[i], 1) if dow_counts[i] else 0 for i in range(7)]
    DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    peak_dow_idx = dow_avg.index(max(dow_avg))

    top_airlines  = dict(sorted(airline_counts.items(),  key=lambda x: x[1], reverse=True)[:12])
    top_ac_types  = dict(sorted(ac_type_counts.items(),  key=lambda x: x[1], reverse=True)[:12])

    return jsonify({
        "daily_average":    daily_avg,
        "busiest_day_date": busiest_date,
        "busiest_day_count": busiest_count,
        "peak_hour":        peak_hour,
        "peak_hour_avg":    hourly_avg[peak_hour],
        "peak_dow":         DOW_NAMES[peak_dow_idx],
        "peak_dow_avg":     dow_avg[peak_dow_idx],
        "days_tracked":     num_days,
        "hourly_avg":       hourly_avg,
        "dow_avg":          dow_avg,
        "airline_breakdown":        top_airlines,
        "aircraft_type_breakdown":  top_ac_types,
        "local_vs_flyover": {"local": local, "flyover": flyover, "unknown": unknown},
        "daily_log":        daily_log,
    })


@api_bp.get("/flights/history/<date>")
def flight_history_day(date):
    history = _load_json(HISTORY_FILE, {})
    if not isinstance(history, dict):
        return jsonify({})
    day = history.get(date, {})
    if not day:
        return jsonify({})

    # Compute per-day stats for the detail page
    hourly = [0] * 24
    airline_counts = {}
    ac_type_counts = {}
    cfg = _cfg()
    home_airport = (cfg.get("location") or {}).get("journey_code", "") if cfg else ""
    local = flyover = unknown = 0
    peak_hour_count = 0

    for flight in day.get("flights", []):
        hour = flight.get("hour")
        if isinstance(hour, int) and 0 <= hour <= 23:
            hourly[hour] += 1
            if hourly[hour] > peak_hour_count:
                peak_hour_count = hourly[hour]

        airline = (flight.get("airline_icao") or "").strip()
        if airline:
            airline_counts[airline] = airline_counts.get(airline, 0) + 1
        else:
            airline_counts["Unknown"] = airline_counts.get("Unknown", 0) + 1

        ac_type = (flight.get("aircraft_type") or "").strip()
        if ac_type:
            ac_type_counts[ac_type] = ac_type_counts.get(ac_type, 0) + 1
        else:
            ac_type_counts["Unknown"] = ac_type_counts.get("Unknown", 0) + 1

        orig = (flight.get("origin") or "").strip()
        dest = (flight.get("destination") or "").strip()
        if not orig and not dest:
            unknown += 1
        elif home_airport and (orig == home_airport or dest == home_airport):
            local += 1
        else:
            flyover += 1

    peak_hour = hourly.index(max(hourly)) if any(hourly) else 0

    return jsonify({
        **day,
        "hourly":                   hourly,
        "peak_hour":                peak_hour,
        "peak_hour_count":          peak_hour_count,
        "airline_breakdown":        dict(sorted(airline_counts.items(), key=lambda x: x[1], reverse=True)[:12]),
        "aircraft_type_breakdown":  dict(sorted(ac_type_counts.items(), key=lambda x: x[1], reverse=True)[:12]),
        "local_vs_flyover":         {"local": local, "flyover": flyover, "unknown": unknown},
    })


# ------------------------------------------------------------------
# System control
# ------------------------------------------------------------------

@api_bp.post("/system/connect-bt")
def connect_bt():
    threading.Thread(
        target=lambda: subprocess.run(["/home/royman/bt-connect.sh"]),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@api_bp.get("/system/bt/devices")
def bt_devices():
    try:
        result = subprocess.run(
            ["bluetoothctl", "paired-devices"],
            capture_output=True, text=True, timeout=5,
        )
        devices = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                devices.append({"mac": parts[1], "name": parts[2]})
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "devices": []}), 500


@api_bp.post("/system/bt/connect")
def bt_connect_device():
    data = request.get_json(force=True)
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "error": "MAC address required"}), 400
    threading.Thread(
        target=lambda: subprocess.run(["bluetoothctl", "connect", mac], capture_output=True),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@api_bp.get("/system/bt/scan")
def bt_scan():
    try:
        paired_result = subprocess.run(
            ["bluetoothctl", "paired-devices"],
            capture_output=True, text=True, timeout=5,
        )
        paired_macs = set()
        for line in paired_result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                paired_macs.add(parts[1])
        try:
            subprocess.run(["bluetoothctl", "scan", "on"], capture_output=True, timeout=8)
        except subprocess.TimeoutExpired:
            pass
        all_result = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True, text=True, timeout=5,
        )
        devices = []
        for line in all_result.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                mac, name = parts[1], parts[2]
                if mac not in paired_macs:
                    devices.append({"mac": mac, "name": name})
        return jsonify({"ok": True, "devices": devices})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "devices": []}), 500


@api_bp.post("/system/bt/pair")
def bt_pair():
    data = request.get_json(force=True)
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "error": "MAC address required"}), 400

    def _pair():
        for cmd in [
            ["bluetoothctl", "pair", mac],
            ["bluetoothctl", "trust", mac],
            ["bluetoothctl", "connect", mac],
        ]:
            subprocess.run(cmd, capture_output=True, timeout=15)

    threading.Thread(target=_pair, daemon=True).start()
    return jsonify({"ok": True})


@api_bp.get("/system/wifi/networks")
def wifi_networks():
    try:
        subprocess.run(
            ["sudo", "nmcli", "dev", "wifi", "rescan"],
            capture_output=True, timeout=8,
        )
        result = subprocess.run(
            ["sudo", "nmcli", "-e", "yes", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        active_result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        )
        current_ssid = ""
        for line in active_result.stdout.splitlines():
            if line.startswith("yes:"):
                current_ssid = line[4:].replace("\\:", ":")
                break
        networks = []
        seen = set()
        for line in result.stdout.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) == 3:
                ssid = parts[0].replace("\\:", ":")
                signal = parts[1]
                security = parts[2]
            elif len(parts) == 2:
                ssid = parts[0].replace("\\:", ":")
                signal = parts[1]
                security = ""
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
                "ssid": ssid,
                "signal": sig_int,
                "security": security,
                "current": ssid == current_ssid,
            })
        networks.sort(key=lambda x: x["signal"], reverse=True)
        return jsonify({"ok": True, "networks": networks, "current": current_ssid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "networks": [], "current": ""}), 500


@api_bp.post("/system/wifi/connect")
def wifi_connect():
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


@api_bp.post("/system/restart-app")
def restart_app():
    threading.Thread(
        target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"])),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@api_bp.post("/system/setup-mode")
def enter_setup_mode():
    """Write the setup flag and restart the service — it will enter setup mode on next boot."""
    try:
        from core.setup_mode import request_setup
        request_setup()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    threading.Thread(
        target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"])),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@api_bp.post("/system/update")
def update_app():
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        updated = output != "Already up to date."
        if updated:
            threading.Thread(
                target=lambda: (time.sleep(1), subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"])),
                daemon=True,
            ).start()
        return jsonify({"ok": True, "updated": updated, "output": output})
    except Exception as e:
        return jsonify({"ok": False, "updated": False, "output": str(e)}), 500


@api_bp.post("/system/reboot")
def reboot_system():
    threading.Thread(
        target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "reboot"])),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@api_bp.post("/system/shutdown")
def shutdown_system():
    threading.Thread(
        target=lambda: (time.sleep(0.5), subprocess.run(["sudo", "shutdown", "-h", "now"])),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# System helpers
# ------------------------------------------------------------------

def _uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        h, rem = divmod(int(secs), 3600)
        m = rem // 60
        return f"{h}h {m}m"
    except Exception:
        return "N/A"


def _cpu_temp() -> str:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f}°C"
    except Exception:
        return "N/A"


def _memory() -> dict:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            info[parts[0].rstrip(":")] = int(parts[1])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        pct = round(used / total * 100) if total else 0
        return {"total_mb": total // 1024, "used_mb": used // 1024, "pct": pct}
    except Exception:
        return {}


def _hostname() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "unknown"
