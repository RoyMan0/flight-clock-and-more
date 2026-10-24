# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An LED matrix display system for a 64×32 RGB LED panel running on a Raspberry Pi. Displays a rotating set of plugins: clock/weather, overhead flight tracker, sports scores, stock ticker, world daylight map, and more. A Flask web UI on port 8080 provides configuration and a live dashboard.

The codebase runs in two modes:
- **Pi (hardware)**: `sudo python3 main.py` — real LED matrix via rpi-rgb-led-matrix
- **Mac/dev (software)**: `python3 main.py --no-hardware` — `rgbmatrix/` is a stub that makes everything import cleanly with no-op hardware calls

## Deployment Workflow

Development is done on Mac; the Pi pulls from GitHub.

**Typical deploy to Pi:**
```bash
# On Mac: commit and push
git add ... && git commit -m "..." && git push

# On Pi:
cd ~/its-a-plane-python && rm data/api_cache.json && git pull && sudo systemctl restart its-a-plane
```

Only delete `data/api_cache.json` when a change affects route/schedule lookups. For UI-only or config changes, skip the `rm`.

**When `requirements.txt` changes**, also run `~/venv/bin/pip install -r requirements.txt` on the Pi before restarting. The service runs `~/venv/bin/python3` (see `utilities/its-a-plane.service.template`). The dashboard "Update" button (`POST /api/system/update`) does this automatically: it hashes `requirements.txt` before/after `git pull` and runs pip with `sys.executable` before restarting. `install.sh` (Phase 3) always installs `requirements.txt`, so fresh installs and re-runs are covered.

**Tail logs on Pi:**
```bash
tail -f workdammit.log
# or for binary-safe grep across rotated logs:
grep -a "pattern" workdammit.log workdammit.log.1 workdammit.log.2
```

**Service management:**
```bash
sudo systemctl restart its-a-plane
sudo systemctl status its-a-plane
sudo systemctl cat its-a-plane   # view service file
```

## Architecture

### Plugin System (`core/plugin_manager.py`)

The main loop runs at 10 fps. Every frame, `_dispatch()` is called:

1. **Clock-only mode check** — if `display.clock_only` is enabled and time is in range, forces `clock_weather` and suppresses all other plugin updates (no API calls)
2. **Priority interrupt** — if `FlightTrackerPlugin.has_live_priority()` AND `has_live_content()` are both True, immediately switches to the flight tracker
3. **Normal rotation** — cycles through `plugin_order` plugins; skips any whose `has_content()` returns False
4. **draw() contract** — `draw()` must return `True` if content was drawn, `False` if nothing to show; the manager only calls `display.swap()` when `draw()` returns True

`_update_loop()` runs in a background thread, calling each plugin's `update()` at its configured `update_interval`. During clock-only mode, only `clock_weather` gets updated.

### BasePlugin Interface (`plugins/base_plugin.py`)

Every plugin inherits from `BasePlugin` and implements:
- `draw() -> bool` — render one frame; return False if nothing to show
- `update()` — fetch data in background thread (never network calls in `__init__`)
- `reset()` — called when plugin becomes active
- `has_live_priority()` / `has_live_content()` — for priority interrupt (only flight tracker uses these)
- `has_content()` — return False to skip this plugin in rotation
- `is_cycle_complete()` — return True to advance to next plugin before `display_duration` expires

### Scene / Animator Pattern

Older display logic uses `Animator` with `@Animator.KeyFrame.add(divisor)` decorators. `divisor=0` runs once on first frame; `divisor=N` runs every N frames. Plugin managers that wrap these scenes use a `tick()` adapter that calls keyframes one at a time instead of the original `play()` infinite loop.

`_FlightDisplay` in `plugins/flight_tracker/manager.py` uses multiple-inheritance to compose several scenes (`FlightDetailsScene`, `JourneyScene`, `PlaneDetailsScene`, etc.) into one display object.

The display is **64×32 pixels**. Constants in `setup/screen.py` (`WIDTH=64`, `HEIGHT=32`). Frame period is `setup/frames.py` (`PERIOD=0.1s`, `PER_SECOND=10`).

### Config System (`core/config_manager.py`)

Two files, both gitignored:
- `config/config.json` — all non-secret settings (bootstrapped from `config.example.json` on first install)
- `config/secrets.json` — API keys, email credentials (see `config/secrets.example.json` for schema)

`ConfigManager` is a singleton (`get_config()`). Plugins receive their config slice via `cfg.get_plugin_config("plugin_id")`. The web UI fires `on_config_change()` callbacks on plugins when settings are saved — plugins don't need to restart to pick up config changes.

`config.py` at the root is a **compatibility shim** that reads from `config.json`/`secrets.json` and exposes constants (e.g. `LOCATION_HOME`, `TOMORROW_API_KEY`). Legacy scenes import from it. New code should use `get_config()` directly.

`TEMPERATURE_LOCATION` is derived at import time from `LOCATION_HOME` — it is not a separate config field. Do not add a `temperature_location` key to `config.json`; it will be ignored.

### Flight Tracker Data Pipeline (`utilities/overhead.py`)

The `Overhead` class fetches aircraft positions and resolves routes in a background thread:

**Position sources (in order, with fallback):**
1. `adsb.lol` — primary, free, no key
2. FR24 anonymous gRPC bounding box — fallback when adsb.lol times out

**Route resolution cascade (per flight, results cached to disk):**
1. FlightStats — free scrape, no key; tried first
2. adsbdb.com — free, no key; skipped for prefixes in `SKIP_ADSBDB_PREFIXES`
3. AirLabs — 1,000 calls/month free; skipped for bare N-tail registrations
4. FlightAware AeroAPI — paid fallback (~$0.005/call, ~$4.50/month budget)
5. FR24 gRPC route — last resort, logged as "(unverified)"

All route results go through `_route_makes_sense()` sanity checking. Bad routes are discarded or replaced with a fallback source.

**Caching:** Route and schedule results are persisted to `data/api_cache.json`. Cache TTL is smart: uses `arr_time + 2h` when arrival time is known, otherwise 6h. "No route" results for flights where only AirLabs was tried get a 30-minute retry window; this is intentional so recently-departed flights can pick up schedule data.

**API key rotation:** Both AirLabs and FlightAware support multiple keys in `secrets.json` as arrays. The active key is selected by preferring the key whose billing reset date is soonest (uses expiring calls first). AirLabs reset days are configurable per-key; FlightAware always resets on the 1st of the month.

### Transit Arrivals Plugin (`plugins/transit/`)

Shows upcoming arrivals, station-sign style (header `Ln | Dest | Min`), for a list of watched stops in `plugins.transit.stops` (each: agency, routes, stop_id, direction, min_minutes, optional header `label`). Stops can be from different agencies.
- `display_mode: "combined"` — static board: next 3 across all stops by time. `"scroll"` — fixed `Ln | Dest | Min` header with every stop's next 3 rows (faint divider between stops, no stop-name line) scrolling bottom-to-top underneath (like `ap_news`), `scroll_speed` px/frame; loops for `display_duration`, which is raised to at least one full pass. A single stop always uses the static board. `"cycle"` (old page-flip mode) is read as `"scroll"`.
- Config page: each stop panel picks one line and one stop from matching list boxes (add panels for more). `routes` stays a list in config; older panels with several lines keep them until a new line is picked.
- `min_minutes` (walk time) never hides arrivals: ones you can't reach are drawn grey (`Arrival.missable`). If all of the next 3 are too soon, the last row shows the next catchable one (`_pick_rows`).
- Legacy single-stop configs (top-level `agency`/`stop_id`) are still read (`configured_stops()` in `manager.py`); the config page upgrades them on load.
- `build_pages()` / `fetch_stop()` in `manager.py` are shared by the plugin and `POST /api/transit/preview`, so the preview matches the panel exactly.
- **Stop pickers list stops in line order** (south → north for N–S lines, west → east for E–W lines, e.g. CTA Red: 95th/Dan Ryan → Howard). `providers/base.py` `merge_sequence`/`order_by_line` merge route patterns; a bus stop's opposite-direction twin (same name) is listed next to it.
  - CTA rail order is a committed file, `plugins/transit/cta_rail_order.json` (not under a `data/` folder — `.gitignore` ignores every `data/`), generated by `scripts/build_cta_rail_order.py` from CTA's 70 MB GTFS (too heavy for a Pi). Re-run it only if stations open or close.
  - CTA bus order comes from Bus Tracker `getpatterns`; GTFS agencies use `route_seq` in the static index (bump `_INDEX_VERSION` in `gtfs_rt.py` whenever the index format changes — that forces a rebuild).

Agencies are declared in `agencies.py`; each maps to a provider in `providers/`:
- `cta.py` — CTA Train Tracker / Bus Tracker JSON APIs (keys required). Station list comes from the City of Chicago open-data "L" stops dataset.
- `gtfs_rt.py` — generic static GTFS + GTFS-realtime (RTD, MTA subway). Static GTFS is reduced once to `data/transit/<agency>/index.json` (rebuilt weekly in a background thread; UI endpoints return 202 "preparing" meanwhile). Adding most new cities = a new registry entry, no code.
  - RTD realtime trips mostly lack `direction_id`; trip IDs match static, so `trip_map: True` recovers direction + headsign.
  - MTA realtime trip IDs do NOT match static; direction comes from the platform stop suffix (`N`/`S`) and destination from the trip's last stop.
- Destinations are shortened by `abbrev.py` (user aliases → built-in per-agency aliases → rules → truncate), never plain-truncated first.
- Config page pickers use `/api/transit/{agencies,routes,stops,directions}` and `POST /api/transit/preview` (renders the actual 64×32 board).

### Web UI (`web/`)

Flask app created by `web/app.py:create_app()`. Two blueprints:
- `web/blueprints/api.py` — REST endpoints under `/api/` (config R/W, metrics, SSE dashboard stream, plugin control)
- `web/blueprints/pages.py` — HTML pages (dashboard, config, flights, about, etc.)

The config page (`web/templates/config.html`) is a single Alpine.js app. All sections load from `/api/system_config`, `/api/secrets` (masked), and `/api/metrics`. Saves are atomic JSON writes.

The dashboard uses SSE (`/api/stream`) for live updates. The Alpine.js `init()` has a `_pollTimer` guard to prevent double-initialization.

## Known States / Gotchas

- **`snow_report` plugin** — disabled/hidden in the web UI until it's fixed. It is filtered out of both the Plugin Order list and the config cards. Do not re-expose it without fixing the underlying issues.
- **Tomorrow.io sunrise/sunset parsing** — the API returns `sunriseTime`/`sunsetTime` with a `Z` suffix, but sometimes the value is already local time (not true UTC), depending on the location's timezone. `ClockScene.calculate_sunrise_sunset()` applies a sanity check: if the parsed-as-UTC value produces a local sunrise hour outside 4am–10am, it reinterprets the value as local time and converts to UTC. `today_sunrise` and `today_sunset` are always UTC-aware `datetime` objects; compare them with `datetime.now(timezone.utc)`, never naive datetimes.
- **OWM API keys** — a 401 from OWM almost always means the key isn't subscribed to an active plan. The code hits the One Call 3.0 endpoint; subscribing to One Call 4.0 at openweathermap.org also activates it. Go to openweathermap.org → My Subscriptions and subscribe the key — the free tier requires this step even though 3.0 calls are free.
- **Pi 3 hotspot / `dtoverlay=disable-bt`** — do NOT add `dtoverlay=disable-bt` to `/boot/firmware/config.txt` on Pi 3. Bluetooth and WiFi share the same chip (CYW43438); disabling BT via that overlay breaks WiFi AP mode, which prevents the first-boot setup hotspot from broadcasting.
- **`rgbmatrix/` directory** — gitignored; it's a Mac dev stub. The real C extension is installed on the Pi only. Never commit the stub.
- **`draw()` must return bool** — `display.swap()` is only called when `draw()` returns `True`. Returning nothing (implicit `None`) is treated as False and will skip the swap, which is intentional for empty-state handling.
- **Airline logos** — PNG files in `logos/` keyed by ICAO airline code. Convert to RGBA on load with `.convert("RGBA")` before any resize to avoid Pillow palette+transparency warnings.
- **Log file** — `workdammit.log` in the project root, written by `RotatingFileHandler` in `main.py` (10MB max, 2 backups). The systemd service file must NOT redirect stdout to this file — the app owns it.
- **Binary content in logs** — the log may contain binary characters from PIL warnings. Use `grep -a` when grepping log files.
- **protobuf pin (fr24 vs gtfs-realtime-bindings)** — `fr24` requires `protobuf<7`; `gtfs-realtime-bindings` 3.x requires `protobuf>=7`. `requirements.txt` pins `gtfs-realtime-bindings==2.0.0` + `protobuf>=6.33,<7` (verified working with fr24 0.3.1 and 0.4.0). Don't bump the bindings until fr24 supports protobuf 7, or the FR24 fallback breaks. Some Pis also have an old `protobuf` in `~/.local`; the venv copy shadows it, and pip's "Not uninstalling … outside environment" message about it is harmless.
- **Mac dev Python** — `/usr/local/bin/python3` is an x86_64 binary that won't run (no Rosetta), and `/usr/bin/python3` is 3.9, which is too old (the code uses `X | None` at module level). Use a 3.10+ interpreter (e.g. a `uv`-managed 3.12 venv) for local testing.
- **Third-party log suppression** — `werkzeug`, `httpx`, and `fr24` loggers are set to WARNING in `main.py` to reduce noise.

## Secrets Schema

`config/secrets.json` fields (see `config/secrets.example.json`):
```json
{
  "tomorrow_api_key": "",
  "airlabs_api_keys": [],
  "airlabs_reset_days": [],
  "flightaware_api_keys": [],
  "flightaware_monthly_budget": 4.50,
  "email_sender": "",
  "email_password": "",
  "schwab_app_key": "",
  "schwab_app_secret": "",
  "schwab_callback_url": "",
  "schwab_token_path": "",
  "owm_api_key": "",
  "cta_train_api_key": "",
  "cta_bus_api_key": "",
  "mta_api_key": ""
}
```

`cta_*` keys are required for Chicago; `mta_api_key` is optional (subway feeds currently work without one).

**Never commit API keys** — the GitHub repo is public. Keys live only in each device's gitignored `config/secrets.json`, entered through the web config page (the transit card has its own key fields; transit keys are hidden from the generic "Other Secrets" card). The owner reuses one pair of CTA keys across their own and friends' Pis, so if a key gets revoked or rate-limited, every one of those Pis is affected.

`flightaware_reset_days` is no longer used — FlightAware always resets on the 1st. The field may exist in older installs but is ignored.
