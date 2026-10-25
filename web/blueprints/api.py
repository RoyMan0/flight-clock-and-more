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

_LED_SCALE = 10                      # preview px per LED
_led_masks = {}
_preview_cache = {"key": None, "data": ""}
_preview_lock = threading.Lock()


def _led_tiles(w, h):
    """White LED dots (for colouring lit pixels) and faint grey dots (unlit pixels)."""
    key = (w, h)
    if key not in _led_masks:
        from PIL import Image, ImageDraw
        s = _LED_SCALE
        dot = Image.new("L", (s, s), 0)
        ImageDraw.Draw(dot).ellipse((1, 1, s - 2, s - 2), fill=255)
        lit = Image.new("RGB", (w * s, h * s), (0, 0, 0))
        unlit = Image.new("RGB", (w * s, h * s), (0, 0, 0))
        white = Image.new("RGB", (s, s), (255, 255, 255))
        grey = Image.new("RGB", (s, s), (24, 24, 26))
        for y in range(h):
            for x in range(w):
                lit.paste(white, (x * s, y * s), dot)
                unlit.paste(grey, (x * s, y * s), dot)
        _led_masks[key] = (lit, unlit)
    return _led_masks[key]


def _preview_data_url(img) -> str:
    """The panel frame drawn as round LEDs (PNG data URL). Identical frames reuse the cache."""
    from PIL import Image, ImageChops
    raw = img.tobytes()
    with _preview_lock:
        if _preview_cache["key"] == raw:
            return _preview_cache["data"]
    lit, unlit = _led_tiles(img.width, img.height)
    big = img.resize((img.width * _LED_SCALE, img.height * _LED_SCALE), Image.NEAREST)
    frame = ImageChops.lighter(ImageChops.multiply(big, lit), unlit)
    buf = io.BytesIO()
    frame.save(buf, format="PNG", optimize=False, compress_level=3)
    data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    with _preview_lock:
        _preview_cache.update(key=raw, data=data)
    return data


@api_bp.get("/display/snapshot")
def display_snapshot():
    d = _display()
    if d is None:
        return jsonify({"error": "display not available"}), 503
    try:
        return jsonify({"image": _preview_data_url(d.get_snapshot())})
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
                    img_data = _preview_data_url(d.get_snapshot())
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
    pin = request.args.get("pin", "1") != "0"     # Dashboard sends pin=0: show once, keep rotating
    ok = pm.force_plugin(plugin_id, pin=pin)
    return jsonify({"ok": ok, "pinned": pin and ok})


@api_bp.post("/display/return")
def return_to_rotation():
    pm = _pm()
    if pm is None:
        return jsonify({"error": "plugin manager not available"}), 503
    pm.release_hold()
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# System configuration
# ------------------------------------------------------------------

@api_bp.get("/config/system")
def get_system_config():
    cfg = _cfg()
    if cfg is None:
        return jsonify({}), 503
    location = dict(cfg.get("location") or {})
    # If timezone isn't in config.json yet, fill in the current OS timezone.
    if not location.get("timezone"):
        location["timezone"] = _read_system_timezone()
    return jsonify({
        "display": cfg.get("display") or {},
        "location": location,
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
    old_tz = (cfg.get("location") or {}).get("timezone", "")
    cfg.save_config(full)

    restarting = False
    tz = (data.get("location") or {}).get("timezone", "").strip()
    if tz:
        _apply_system_timezone(tz)
        if tz != old_tz:
            # Timezone changed — need a full process restart so datetime.now()
            # picks up the new /etc/localtime. Schedule it after the response.
            restarting = True
            import threading as _th
            def _delayed_restart():
                import time as _t
                _t.sleep(1.5)
                subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"],
                               capture_output=True)
            _th.Thread(target=_delayed_restart, daemon=True).start()

    return jsonify({"ok": True, "restarting": restarting})


def _read_system_timezone() -> str:
    """Read the OS timezone without requiring any subprocess."""
    # /etc/timezone is the canonical source on Debian/Pi OS (plain text file)
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except Exception:
        pass
    # Fallback: /etc/localtime symlink → /usr/share/zoneinfo/America/Denver
    try:
        import os as _os
        link = _os.readlink("/etc/localtime")
        idx = link.find("/zoneinfo/")
        if idx >= 0:
            return link[idx + 10:]
    except Exception:
        pass
    # Last resort: timedatectl
    try:
        r = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        tz = r.stdout.strip()
        if tz:
            return tz
    except Exception:
        pass
    return "UTC"


def _apply_system_timezone(tz: str):
    r = subprocess.run(
        ["sudo", "timedatectl", "set-timezone", tz],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        log.warning(f"[tz] timedatectl failed (rc={r.returncode}): {r.stderr.strip()}")


@api_bp.get("/system/timezone")
def get_system_timezone():
    """Return the current OS timezone."""
    tz = _read_system_timezone()
    # Also check config in case it overrides the OS
    if not tz:
        cfg = _cfg()
        tz = (cfg.get("location") or {}).get("timezone", "UTC") if cfg else "UTC"
    return jsonify({"timezone": tz})


def _list_timezones() -> list[str]:
    """Return sorted list of IANA timezone names."""
    try:
        from zoneinfo import available_timezones
        return sorted(available_timezones())
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["timedatectl", "list-timezones"],
            capture_output=True, text=True, timeout=10,
        )
        zones = [z.strip() for z in result.stdout.splitlines() if z.strip()]
        if zones:
            return sorted(zones)
    except Exception:
        pass
    # Curated fallback covering most common zones
    return sorted([
        "UTC",
        "America/Anchorage","America/Chicago","America/Denver","America/Detroit",
        "America/Los_Angeles","America/New_York","America/Phoenix","America/Honolulu",
        "America/Toronto","America/Vancouver","America/Mexico_City","America/Sao_Paulo",
        "America/Argentina/Buenos_Aires","America/Bogota","America/Lima",
        "Europe/London","Europe/Dublin","Europe/Lisbon","Europe/Paris","Europe/Berlin",
        "Europe/Madrid","Europe/Rome","Europe/Amsterdam","Europe/Brussels",
        "Europe/Stockholm","Europe/Oslo","Europe/Copenhagen","Europe/Helsinki",
        "Europe/Warsaw","Europe/Prague","Europe/Vienna","Europe/Zurich",
        "Europe/Athens","Europe/Istanbul","Europe/Moscow","Europe/Kiev",
        "Asia/Dubai","Asia/Karachi","Asia/Kolkata","Asia/Dhaka","Asia/Bangkok",
        "Asia/Singapore","Asia/Hong_Kong","Asia/Shanghai","Asia/Tokyo","Asia/Seoul",
        "Australia/Sydney","Australia/Melbourne","Australia/Brisbane","Australia/Perth",
        "Pacific/Auckland","Pacific/Fiji","Pacific/Honolulu",
        "Africa/Cairo","Africa/Johannesburg","Africa/Lagos","Africa/Nairobi",
    ])


@api_bp.get("/system/timezones")
def list_timezones():
    zones = _list_timezones()
    # Group by first component for the UI
    grouped: dict[str, list[str]] = {}
    for z in zones:
        region = z.split("/")[0] if "/" in z else "Other"
        grouped.setdefault(region, []).append(z)
    return jsonify({"zones": zones, "grouped": grouped})


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
    return jsonify({
        "airlabs":     get_al_metrics(al_keys, al_reset_days),
        "flightaware": get_fa_metrics(fa_keys, budget),
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
        ("FA Cup",       f"{ESPN}/soccer/eng.fa/teams",                         False),
        ("Championship", f"{ESPN}/soccer/eng.2/teams",                          False),
        ("Bundesliga",   f"{ESPN}/soccer/ger.1/teams",                          False),
        ("UCL",          f"{ESPN}/soccer/uefa.champions/teams",                 False),
        ("Europa League",f"{ESPN}/soccer/uefa.europa/teams",                    False),
        ("La Liga",      f"{ESPN}/soccer/esp.1/teams",                          False),
        ("Ligue 1",      f"{ESPN}/soccer/fra.1/teams",                          False),
        ("Serie A",      f"{ESPN}/soccer/ita.1/teams",                          False),
        ("World Cup",    f"{ESPNW}/soccer/fifa.world/teams",                    False),
        ("Copa América", f"{ESPNW}/soccer/conmebol.copa/teams",                 False),
        ("Gold Cup",     f"{ESPNW}/soccer/concacaf.gold/teams",                 False),
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
# Transit arrivals — cascading pickers for the config page
# ------------------------------------------------------------------

def _transit_call(fn):
    """Run a provider call, mapping 'still preparing' / setup errors to JSON."""
    from plugins.transit.providers.base import ProviderConfigError, ProviderNotReady
    try:
        return jsonify({"status": "ok", "items": fn()})
    except ProviderNotReady as e:
        return jsonify({"status": "preparing", "message": str(e)}), 202
    except (ProviderConfigError, ValueError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Request failed: {e}"}), 502


def _transit_provider():
    from plugins.transit.agencies import get_provider
    prov = get_provider(request.args.get("agency", ""))
    if prov is None:
        raise ValueError("unknown agency")
    return prov


def _transit_routes_arg():
    return [r for r in request.args.get("routes", "").split(",") if r]


@api_bp.get("/transit/agencies")
def transit_agencies():
    from plugins.transit.agencies import list_agencies
    return jsonify(list_agencies())


@api_bp.get("/transit/routes")
def transit_routes():
    return _transit_call(lambda: _transit_provider().list_routes())


@api_bp.get("/transit/stops")
def transit_stops():
    return _transit_call(lambda: _transit_provider().list_stops(_transit_routes_arg()))


@api_bp.get("/transit/directions")
def transit_directions():
    return _transit_call(lambda: _transit_provider().list_directions(
        request.args.get("stop", ""), _transit_routes_arg()))


@api_bp.post("/transit/preview")
def transit_preview():
    """Fetch live arrivals for an unsaved transit config and render each 64×32 page."""
    from PIL import Image
    from plugins.transit.manager import (
        build_pages, configured_stops, fetch_stop, is_scroll_mode, line_label_width,
    )
    from plugins.transit.providers.base import ProviderNotReady
    from plugins.transit.render import (
        SCROLL_Y, build_scroll_strip, render_board, render_scroll_frame,
    )

    def _png(img):
        buf = io.BytesIO()
        img.resize((img.width * 6, img.height * 6), Image.NEAREST).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    data = request.get_json(force=True) or {}

    def _run():
        stops = configured_stops(data)
        if not stops:
            raise ValueError("Add at least one stop first")
        results, problems, preparing = [], [], False
        for stop in stops:
            name = stop.get("stop_name") or stop.get("stop_id")
            try:
                results.append((stop, fetch_stop(stop)))
            except ProviderNotReady as e:
                preparing = True
                problems.append(f"{name}: {e}")
            except Exception as e:
                problems.append(f"{name}: {e}")
        if not results and preparing:
            raise ProviderNotReady("; ".join(problems))

        built = build_pages(data, results, line_label_width(stops))
        scrolling = is_scroll_mode(data) and len(built) > 1
        pages = []
        for title, ln_w, rows in built:
            pages.append({
                "title": title or "",
                "rows": [{"line": r["label"], "dest_full": r["dest_full"], "dest": r["dest"],
                          "min": r["min"], "missable": r["missable"]} for r in rows],
                # Scroll mode gets one tall image of the whole loop instead (below)
                "image": None if scrolling else _png(render_board(ln_w, rows)),
            })
        scroll_image = None
        if scrolling:
            ln_w = built[0][1]
            strip = build_scroll_strip(ln_w, [(t, rows) for t, _w, rows in built])
            tall = Image.new("RGB", (64, SCROLL_Y + strip.height))
            tall.paste(render_scroll_frame(ln_w, strip, 0).crop((0, 0, 64, SCROLL_Y)), (0, 0))
            tall.paste(strip, (0, SCROLL_Y))
            scroll_image = _png(tall)
        return {"pages": pages, "scroll_image": scroll_image, "problems": problems}

    return _transit_call(_run)


# ------------------------------------------------------------------
# World clock — city picker + preview for the config page
# ------------------------------------------------------------------

@api_bp.get("/world_clock/cities")
def world_clock_cities():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from plugins.world_clock.cities import CITIES, display_name

    now = datetime.now(timezone.utc)
    out = []
    for c in CITIES:
        try:
            mins = int(now.astimezone(ZoneInfo(c["tz"])).utcoffset().total_seconds() // 60)
        except Exception:
            continue
        sign = "+" if mins >= 0 else "-"
        h, m = divmod(abs(mins), 60)
        offset = f"UTC{sign}{h}" + (f":{m:02d}" if m else "") if mins else "UTC"
        out.append({"id": c["id"], "name": c["name"], "short": display_name(c),
                    "country": c["country"], "offset": offset, "offset_min": mins})
    out.sort(key=lambda c: c["name"].lower())
    return jsonify(out)


@api_bp.post("/world_clock/preview")
def world_clock_preview():
    """Render an unsaved world clock config: the board, or the whole scroll loop."""
    from PIL import Image
    from plugins.world_clock.manager import build_rows, build_strip, needs_scroll, render_board

    data = request.get_json(force=True) or {}
    rows = build_rows(data)
    if not rows:
        return jsonify({"status": "error", "message": "Select at least one city"}), 400
    img = build_strip(rows) if needs_scroll(rows) else render_board(rows)
    buf = io.BytesIO()
    img.resize((img.width * 6, img.height * 6), Image.NEAREST).save(buf, format="PNG")
    return jsonify({
        "status": "ok",
        "scrolling": needs_scroll(rows),
        "image": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(),
    })


# ------------------------------------------------------------------
# Now Playing — Spotify connect, Mac SSH key, test/preview
# ------------------------------------------------------------------

@api_bp.get("/now_playing/spotify/auth_url")
def now_playing_spotify_auth_url():
    from plugins.now_playing.sources import spotify
    try:
        return jsonify({"ok": True, "url": spotify.auth_url(), "redirect_uri": spotify.REDIRECT_URI})
    except spotify.SpotifyError as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@api_bp.post("/now_playing/spotify/callback")
def now_playing_spotify_callback():
    from plugins.now_playing.sources import spotify
    data = request.get_json(force=True) or {}
    try:
        return jsonify({"ok": True, "user": spotify.complete_auth(data.get("url", ""))})
    except spotify.SpotifyError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"Sign-in failed: {e}"}), 502


@api_bp.get("/now_playing/spotify/status")
def now_playing_spotify_status():
    from plugins.now_playing.sources import spotify
    if not spotify.is_connected():
        return jsonify({"connected": False, "has_client_id": bool(spotify.client_id())})
    try:
        return jsonify({"connected": True, "has_client_id": True, "user": spotify.account_name()})
    except Exception as e:
        return jsonify({"connected": False, "has_client_id": True, "message": str(e)})


@api_bp.get("/now_playing/mac/key")
def now_playing_mac_key():
    from plugins.now_playing.sources import mac_music
    try:
        key = mac_music.ensure_key()
    except Exception as e:
        return jsonify({"ok": False, "message": f"Could not create an SSH key: {e}"}), 500
    cmd = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '{key}' >> ~/.ssh/authorized_keys"
    return jsonify({"ok": True, "key": key, "command": cmd})


@api_bp.post("/now_playing/test")
def now_playing_test():
    """Check each enabled source for an unsaved config; render the frame that would show."""
    from PIL import Image
    from plugins.now_playing.manager import first_playing
    from plugins.now_playing.render import make_marquees, render_frame

    data = request.get_json(force=True) or {}
    statuses = []
    track = first_playing(data, statuses)
    image = None
    if track:
        dur, pos = track.get("duration_ms"), track.get("progress_ms")
        frame = render_frame(*make_marquees(track), track.get("art"),
                             (pos / dur) if dur and pos is not None else None)
        buf = io.BytesIO()
        frame.resize((64 * 6, 32 * 6), Image.NEAREST).save(buf, format="PNG")
        image = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    return jsonify({"statuses": statuses, "image": image,
                    "playing": f"{track['title']} — {track['artist']}" if track else None})


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
                bucket = "Private/GA" if (flight.get("airline") or "").strip() else "Unknown"
                airline_counts[bucket] = airline_counts.get(bucket, 0) + 1

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
            bucket = "Private/GA" if (flight.get("airline") or "").strip() else "Unknown"
            airline_counts[bucket] = airline_counts.get(bucket, 0) + 1

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
    """Reconnect Bluetooth audio. Runs ~/bt-connect.sh for the app's user if one exists
    (custom per-device setups); otherwise reconnects every paired device via bluetoothctl,
    so this works on any install regardless of username."""
    custom = os.path.expanduser("~/bt-connect.sh")

    def _connect():
        if os.path.isfile(custom) and os.access(custom, os.X_OK):
            subprocess.run([custom], capture_output=True, timeout=60)
            return
        listing = subprocess.run(["bluetoothctl", "devices", "Paired"],
                                 capture_output=True, text=True, timeout=5)
        if listing.returncode != 0 or not listing.stdout.strip():
            listing = subprocess.run(["bluetoothctl", "paired-devices"],     # older BlueZ
                                     capture_output=True, text=True, timeout=5)
        for line in listing.stdout.splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                subprocess.run(["bluetoothctl", "connect", parts[1]], capture_output=True, timeout=15)

    threading.Thread(target=_connect, daemon=True).start()
    return jsonify({"ok": True, "mode": "script" if os.path.isfile(custom) else "paired-devices"})


@api_bp.get("/system/bt/devices")
def bt_devices():
    import re
    _ansi = re.compile(r'\x1b\[[0-9;]*[mK]|\x1b\[[?][0-9]*[lh]|\r')
    def _parse(output):
        devices = []
        for line in output.splitlines():
            line = _ansi.sub('', line).strip()
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                devices.append({"mac": parts[1], "name": parts[2]})
        return devices
    def _macs(output):
        macs = set()
        for line in output.splitlines():
            line = _ansi.sub('', line).strip()
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                macs.add(parts[1])
        return macs
    try:
        result = subprocess.run(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True, text=True, timeout=5,
        )
        devices = _parse(result.stdout)
        if not devices and result.returncode != 0:
            result = subprocess.run(
                ["bluetoothctl", "paired-devices"],
                capture_output=True, text=True, timeout=5,
            )
            devices = _parse(result.stdout)
        conn_result = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=5,
        )
        connected = _macs(conn_result.stdout)
        for d in devices:
            d["connected"] = d["mac"] in connected
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
    import re
    _ansi = re.compile(r'\x1b\[[0-9;]*[mK]|\x1b\[[?][0-9]*[lh]|\r')
    def _parse_macs(output):
        macs = set()
        for line in output.splitlines():
            line = _ansi.sub('', line).strip()
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device":
                macs.add(parts[1])
        return macs
    def _parse_devices(output, exclude):
        devices = []
        for line in output.splitlines():
            line = _ansi.sub('', line).strip()
            parts = line.split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                mac, name = parts[1], parts[2]
                if mac not in exclude:
                    devices.append({"mac": mac, "name": name})
        return devices
    try:
        paired = subprocess.run(
            ["bluetoothctl", "devices", "Paired"],
            capture_output=True, text=True, timeout=5,
        )
        paired_macs = _parse_macs(paired.stdout)
        if not paired_macs:
            fallback = subprocess.run(
                ["bluetoothctl", "paired-devices"],
                capture_output=True, text=True, timeout=5,
            )
            paired_macs = _parse_macs(fallback.stdout)
        try:
            subprocess.run(["bluetoothctl", "scan", "on"], capture_output=True, timeout=8)
        except subprocess.TimeoutExpired:
            pass
        all_result = subprocess.run(
            ["bluetoothctl", "devices"],
            capture_output=True, text=True, timeout=5,
        )
        devices = _parse_devices(all_result.stdout, paired_macs)
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


@api_bp.get("/system/check-updates")
def check_updates():
    try:
        subprocess.run(["git", "fetch"], cwd=BASE_DIR, capture_output=True, timeout=15)
        result = subprocess.run(
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=10,
        )
        count = int(result.stdout.strip() or "0")
        return jsonify({"ok": True, "has_updates": count > 0, "count": count})
    except Exception as e:
        return jsonify({"ok": False, "has_updates": False, "error": str(e)}), 500


def _requirements_hash():
    import hashlib
    try:
        with open(os.path.join(BASE_DIR, "requirements.txt"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _install_requirements_then_restart(deps_changed: bool):
    import logging
    import sys
    log = logging.getLogger(__name__)
    time.sleep(1)
    if deps_changed:
        # The service runs from the venv, so sys.executable's pip installs into it
        log.info("[update] requirements.txt changed — installing dependencies")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r",
             os.path.join(BASE_DIR, "requirements.txt")],
            capture_output=True, text=True, timeout=900,
        )
        if r.returncode != 0:
            log.warning(f"[update] pip install failed: {r.stderr.strip()[-2000:]}")
    subprocess.run(["sudo", "systemctl", "restart", "its-a-plane"])


@api_bp.post("/system/update")
def update_app():
    try:
        req_before = _requirements_hash()
        result = subprocess.run(
            ["git", "pull"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        updated = output != "Already up to date."
        deps_changed = False
        if updated:
            deps_changed = _requirements_hash() != req_before
            if deps_changed:
                output += "\nInstalling updated Python dependencies before restart (may take a few minutes)…"
            threading.Thread(
                target=_install_requirements_then_restart,
                args=(deps_changed,),
                daemon=True,
            ).start()
        return jsonify({"ok": True, "updated": updated, "deps_changed": deps_changed, "output": output})
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
