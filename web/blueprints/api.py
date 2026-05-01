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
    if cfg is None:
        return jsonify({"error": "config not available"}), 503
    data = request.get_json(force=True)
    cfg.save_plugin_config(plugin_id, data)
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
        ("MLS",       f"{ESPN}/soccer/usa.1/teams",                          False),
        ("EPL",       f"{ESPN}/soccer/eng.1/teams",                          False),
        ("UCL",       f"{ESPN}/soccer/uefa.champions/teams",                 False),
        ("La Liga",   f"{ESPN}/soccer/esp.1/teams",                          False),
        ("World Cup", f"{ESPNW}/soccer/fifa.world/teams",                    False),
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
