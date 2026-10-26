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

0. **Pinned plugin** (Display page) wins over everything below.
1. **Clock-only mode check** — if `display.clock_only` is enabled and time is in range, forces `clock_weather` and suppresses all other plugin updates (no API calls)
2. **Live priority takeover** — `_find_live("takeover")` checks the flight tracker first, then `plugin_order`, then any other registered plugin. The first enabled plugin whose `has_live_priority()` and `has_live_content()` are both True and whose `live_priority_mode()` is `"takeover"` preempts the rotation for as long as both stay True (`_priority_id`; `display_duration` is ignored meanwhile). When it ends, the rotation resumes with the module it interrupted. An **alert** is a takeover whose plugin clears `has_live_content()` itself after one pass (sports score alerts, calendar reminders).
3. **Normal rotation** — cycles through `plugin_order` plugins and skips any whose `has_content()` returns False. When a module finishes, `_advance_rotation` first slots in a live `"interleave"` plugin (`_interleaving`) without moving `_rotation_index`. The interleaved plugin's own turn right after that is skipped, so it isn't shown twice in a row.
   - A plugin shown as a priority (takeover or interleave) gets `on_priority_view(True)` before `reset()`; normal turns get `False`. Sports uses this to show only live games; Calendar uses it to show the reminder card.
   - `has_live_content()` runs every frame for plugins with priority on, so keep it cheap (Calendar caches its check for 1 s).
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

### Transit Arrivals/Departures Plugin (`plugins/transit/`)

Shows upcoming arrivals, station-sign style (header `Ln | Dest | Min`), for a list of watched stops in `plugins.transit.stops` (each: agency, routes, stop_id, direction, min_minutes, optional header `label`). Stops can be from different agencies. A grey separator (`SEPARATOR_Y = 7`, same as AI Usage) runs under the header in both modes; scroll mode's window starts at `SCROLL_Y = 9`.
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

### World Clock Plugin (`plugins/world_clock/`)

Current time in any number of user-chosen cities: four 8 px rows (no header), city name left, time right. With more than four cities the list scrolls bottom-to-top like `ap_news` (`scroll_speed` px/frame, a dashed divider marks where the list loops; `display_duration` is raised to at least one pass).
- Config: `plugins.world_clock.cities` is an **ordered** list of `{"id", "label"}` (ids from `cities.py`; `label` overrides the display name). The config page has a filtered checkbox list to add cities and ▲/▼ to reorder.
- `cities.py` — ~230 cities with IANA tz + lat/lon, covering every inhabited UTC offset (incl. :30/:45 zones). `short` names must be ≤ 9 chars (the column width without a day marker).
- Time colour = LIGHT_ORANGE when the sun is up in that city, LIGHT_BLUE when down (same colours as `ClockScene`), from the real sun position via `utilities/solar.is_daylight()` — not the `NIGHT_START`/`NIGHT_END` window.
- Follows the system `location.clock_format` (12hr → `3:45P`, like the world_daylight overlay). A dim compact "+1"/"-1" (hand-drawn mini glyphs, 6 px) marks a different date from the Pi's local date.
- Reuses `plugins/transit/render.py` BDF helpers and `plugins/transit/abbrev.shorten()` for over-long names. No network calls; `update()` is a no-op.
- `utilities/solar.py` holds the sun-position math shared with `world_daylight` (moved there unchanged).

### Now Playing Plugin (`plugins/now_playing/`)

Current song: title marquee across the top (green, 5x7 font), 21×21 artwork bottom-left (1 px margin on top and left; progress bar full width), artist/album to its right (each scrolls if > 40 px), 1 px progress bar on row 31. Takes its normal rotation turn; `has_content()` is False when nothing is playing (or no update for 30 s).
- Sources are checked in the user's `source_order`; the **first one playing wins**. Each source lives in `sources/`:
  - `spotify.py` — Web API `currently-playing` (any of the user's devices). Auth is **PKCE with a paste-back flow**: redirect URI `http://127.0.0.1:8888/callback` (Spotify only allows plain-http redirects to loopback IPs, and the Pi is reached by LAN IP), so the user pastes the failed-to-load callback URL into the config page. `spotify_refresh_token` is rewritten whenever Spotify rotates it.
  - `mac_music.py` — Music app on the user's Mac via `ssh … osascript -` (script on stdin). Key in `data/now_playing/id_ed25519` (gitignored, per-Pi), own `known_hosts`, ControlMaster keeps polling cheap. Only touches Music if it's already running. First run needs the Mac's Automation prompt ("sshd-keygen-wrapper wants to control Music") approved — until then osascript blocks and the source times out. Artwork is fetched only when the track's persistent ID changes.
  - `lastfm.py` — `user.getrecenttracks` "now playing" (catch-all for scrobbling devices, e.g. iPhone Apple Music via a scrobbler app). No progress data → no bar. Grey-star placeholder art is ignored.
- There is **no public live now-playing API for Apple Music on iPhone**; that's why Mac (live) + Last.fm exist. Don't add a background helper on the Mac — the user rejected that.
- `manager.first_playing()` / `attach_art()` are shared with `POST /api/now_playing/test`. Artwork is cached per track (30 entries).

### System Stats Plugin (`plugins/system_stats/`)

CPU/GPU %+temp, memory % (optional uptime, disk and network I/O) for this Pi and any number of machines over SSH (`plugins.system_stats.connections`, each with its own `metrics`, `disk_max`/`net_max`). Layout: hostname line (uptime right-aligned on it when enabled — it is not a stat row) + 8 px stat rows (text rows 0–4, gap, 1 px bar on row 6); one machine with ≤3 stats is static, otherwise a scrolling strip with dividers.
- `collect.py`: one read-only shell script per poll (`sh -s` locally or over SSH), parsed into cumulative counters; `compute()` turns two snapshots into CPU % and bytes/s, so rates are averages over the refresh interval and the first poll has none. Counter resets clamp to 0.
  - Linux: physical disks only (`sd*`, `nvme*n*`, `mmcblk*`, `vd*` — no partitions/`md`/`dm`), only NICs with `/sys/class/net/<if>/device`; CPU temp prefers `x86_pkg_temp` → hwmon coretemp/k10temp/cpu_thermal → CPU thermal zone (ignores `acpitz`, drive sensors). GPU only via `nvidia-smi` (the Pi has none).
  - macOS: `top -l 2` (≈1.8 s), `memory_pressure`, ioreg block-storage read/write bytes, `netstat -ibn` en* (parse byte columns from the right — Address can be blank), ioreg GPU utilisation. **No temperatures without admin** unless `smctemp`/`osx-cpu-temp` is installed.
- `manager.update()` is **fire-and-forget**: it submits one background poll per machine and returns at once (the plugin manager runs every plugin's update on a single thread). A machine with a poll still running is skipped.
- Disk/net bars: log scale from 1 KB/s to the connection's full-scale (defaults 7 GB/s disk, 10 Gb/s network).
- SSH is the shared `utilities/ssh_remote.py` (key `data/ssh/id_ed25519`, migrated from `data/now_playing/` so already-trusted machines keep working; `GET /api/ssh/key`). Now Playing's Mac source uses the same helper.

### Sports live priority (`plugins/sports/manager.py`)

`live_priority` (on/off) plus `live_priority_mode`:
- `"alert"` (default): a game starting or a score changing sets `_alert_pending`, reusing the existing `_prev_scores` detection that drives the ESPN sound. Games already live at startup don't count (`_first_update`). Any full pass that started while an alert was pending clears it (`_pass_clears_alert`).
- `"interleave"`: live while any game is live; `live_priority_mode()` returns `"interleave"`.
- `"takeover"`: live while any game is live.

In priority view `draw()` shows only the live games. Before this change, `has_live_priority`/`has_live_content` existed but were never called: the manager only knew about the flight tracker.

### Calendar Plugin (`plugins/calendar/`)

Upcoming events from `plugins.calendar.sources`. Each source is `{uid, type: "ics"|"caldav", name, color, enabled, server_url, username, calendars:[{href,name,color,enabled}]}`.
- **Secrets:** the ICS link or CalDAV app password lives in **secrets.json** as `calendar_url_<uid>` / `calendar_pw_<uid>`, never in config. Read it at fetch time via `get_config().get_secret()`. The config page hides `calendar_*` from "Other Secrets". `/api/calendar/*` treats a masked or blank field as "use the saved value". Removing a source leaves its secret behind (`save_secrets` never deletes keys); harmless.
- **`sources.py` — ICS:** `webcal://` is fetched as `https://`.
- **`sources.py` — CalDAV:** written by hand with requests + ElementTree, so lxml isn't needed on the Pi.
  - Discovery runs principal → `calendar-home-set` → Depth-1 calendar list (VEVENT calendars only, `apple:calendar-color`), then a `calendar-query` REPORT with a time range.
  - Redirects are followed manually, because requests turns a 302 PROPFIND into a GET and drops auth on iCloud's `pNN-` shard redirect.
  - The calendar-home URL is cached per (server, user).
  - Presets: iCloud `https://caldav.icloud.com/` (Apple ID plus app-specific password), Fastmail, Yahoo.
- **Parsing:** both paths go through `parse_ics`, where `icalendar` + `recurring_ical_events` expand RRULE, EXDATE and RECURRENCE-ID overrides within `window(days_ahead)` (local midnight + N days). Cancelled events are dropped. Timed events become aware local datetimes; all-day events keep `date` with an exclusive end. The libraries are imported lazily, so a Pi that hasn't run pip gets a readable error.
- **`manager.upcoming()`:** drops ended events, de-dupes (uid, start, title), sorts all-day first within each day, caps at `max_events`. `update()` runs one thread per source and keeps the last good events on error. `on_config_change` refetches immediately. `enabled` defaults to False.
- **Detailed mode (`render.py`):** header (7×5 icon in the calendar colour plus the calendar name; for CalDAV the calendar's own name) and 24 px panels.
  - Time column x1–22: start over a dim end (`7:30P`/`-9:30P`, `19:30`/`-21:30`; the dash is dropped when it won't fit, e.g. `10:30P`), `ALL`/`DAY`, or `NOW`/end. Times use `_TIME_FONT`, a 4x6 copy whose colon is shifted left with a 2 px advance (1 px each side), so `10:30P` fits.
  - Bar at x23; text from x25, with the title wrapped to 2 lines, then the location if there's room, then TODAY/TOMORROW/`FRI 10/30`.
  - `events_per_visit` events per turn, each held `seconds_per_event`, sliding 2 px/frame between them. `display_duration` = n × hold + (n−1) × slide; when shown longer (pinned, or the only module with content) it slides from the last event back to the first and keeps looping.
- **Compact mode:** header `CALENDAR` plus the date, 8 px rows (coloured time in `_TIME_FONT`, title from x24), and a dim divider row per new day (the first only if it isn't today). Static if ≤3 rows; otherwise a looping strip like transit with `scroll_speed`.
- **Reminders** (`reminders`, `reminder_minutes`): takeover alert.
  - `has_live_content()` is True for an unreminded timed event starting within N minutes (or up to 1 min late).
  - The card's header reads `IN N MIN` in yellow for `REMINDER_SECONDS` (15). The event is then recorded in `_reminded` (pruned after 2 days) and the plugin goes un-live.
- **Endpoints:** `POST /api/calendar/test` (statuses plus a preview) and `POST /api/calendar/caldav/calendars` (the Find calendars button).
- **Dependencies:** `icalendar` and `recurring-ical-events` are in `requirements.txt`. The web Update button pip-installs them when requirements change; a manual `git pull` needs `pip install -r requirements.txt` in the service's venv.

### AI Usage Plugin (`plugins/ai_usage/`)

Claude and ChatGPT Codex subscription limits: one page per enabled provider (`seconds_per_provider` each). Each page shows a 5×5 logo with a header, `Current` (the 5-hour window) and `Weekly`, each with a 2 px bar, and a tom-thumb reset line. `reset_format` is `times`, `countdown`, `both` (alternates every 3 s) or `off` (3 px bars, more spacing). Bar colours are `render.BAR_LEVELS`: green <50, yellow 50–74, orange 75–89, red ≥90.
- **Data is free but undocumented.** Claude uses `GET api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`, fields `five_hour`/`seven_day` → `utilization`, ISO `resets_at`). Codex uses `GET chatgpt.com/backend-api/wham/usage` (`ChatGPT-Account-Id` header, `rate_limit.primary_window`/`secondary_window` → `used_percent`, epoch `reset_at`). Constants are in `providers.py`. The Claude token URL and client ID were taken from the Claude Code binary (`platform.claude.com/v1/oauth/token`); re-check there if refresh starts failing.
- **Auth = the CLIs' own login files on the Pi:** `~/.claude/.credentials.json` (`claude auth login`) and `~/.codex/auth.json` (`codex login --device-auth`). If the configured path is blank, it uses `~` and then `/home/*/…`, since the service may run as root. `claude setup-token` tokens **can't** be used because they lack the `user:profile` scope.
- **Refresh tokens rotate on every refresh.** `providers.py` refreshes when <5 min remain (Claude: `expiresAt` in ms; Codex: JWT `exp`) or on a 401. It then writes the new tokens back atomically, keeping the file's mode and owner. A per-provider lock stops the plugin poll and the config page's Test button from refreshing at the same time. Never point it at a credentials file copied from another machine that's still in use: the two would invalidate each other.
- The plugin overrides `enabled` to default **False**, because existing installs have no `ai_usage` block and the update loop polls every enabled plugin. `update()` runs one fire-and-forget thread per provider; `reset()` (each time the module comes on screen) also triggers one unless that provider was fetched <3 min ago (`SHOW_REFRESH_MIN_AGE`). The usage endpoints rate-limit hard (Claude 429s when polled every 1–5 min), so a 429 pauses that provider for `Retry-After` or 5 min, then 10 min if it repeats, never longer, since staler numbers aren't useful (`_back_off`); both the timer and the on-screen refresh respect it, and the last numbers stay on screen. On errors it keeps the last good values. The cache is `data/ai_usage.json`. A window whose `resets_at` has passed shows 0 %.
- `POST /api/ai_usage/test` fetches with the unsaved settings and returns statuses plus previews.

### Maze Plugin (`plugins/maze/`)

Display mode: the Pi generates and solves a maze in its rotation slot; play mode: the user plays it from the Display tab.
- `generate.py` — cell grid with open-side bitmasks; `Layout` maps S/M/L to 1/2/3 px paths with 1 px walls (31×15, 21×10, 15×7 cells). Styles: winding (backtracker), branchy (Prim's), loops (braided — Pac-Man always uses this). Exit = farthest cell from the start. Seeded and fast (<2 ms).
- `solve.py` — step-machine solvers (`step()` per tick, `cells`/`head`/`path`/`dead_ends`): Explorer (new route colour after every dead end, failed branches kept dimmed), WallFollower (hands over to Explorer if it detects a loop), Flood (BFS layer per step, then traces the shortest path). The plugin keeps solver state between rotation turns, so a maze only changes after it's solved (+ celebration + stats card).
- `game.py` — one shared play session: free / race (Explorer as the Pi racer) / pacman (dots, ghosts chase within 6 cells, 3 lives) / fog (Chebyshev radius). Hints, give-up (Flood from the player), best times top-5 in `data/maze/best.json` (hinted wins aren't recorded). `version` bumps on change for cheap polling.
- Play sessions: `POST /api/maze/start` calls `start_game(…, on_end=pm.release_hold, still_pinned=…)` then pins the plugin. The plugin ends the game on idle (`play_idle_minutes`, releases the pin) or when something else unpinned it (Return to Rotation). The Display page draws the maze client-side on a `<canvas>` from JSON (moves feel instant) and polls `/api/maze/state?v=` every 150 ms; the panel mirrors it via `render_play`.
- `render.py` caches each maze's wall layer (`Canvas.with_walls`), so frames only paint routes/markers.

### Web UI (`web/`)

Flask app created by `web/app.py:create_app()`. Two blueprints:
- `web/blueprints/api.py` — REST endpoints under `/api/` (config R/W, metrics, SSE dashboard stream, plugin control)
- `web/blueprints/pages.py` — HTML pages (dashboard, config, flights, about, etc.)

The config page (`web/templates/config.html`) is a single Alpine.js app. All sections load from `/api/system_config`, `/api/secrets` (masked), and `/api/metrics`. Saves are atomic JSON writes.

**Live preview (Dashboard + Display page).** The rgbmatrix canvas can't be read back, so `core/canvas_mirror.py` wraps it: `DisplayManager.canvas` is a `MirrorCanvas` that forwards every `SetPixel`/`Clear`/`Fill`/`SetImage` to the panel and keeps a 64×32 copy, and `install()` (called first thing in `main.py`, before `setup/fonts.py` creates fonts) wraps `rgbmatrix.graphics.DrawText/DrawLine/DrawCircle/Font` so the C library still draws to the real canvas while the same glyphs/lines are drawn into the copy (glyph placement from `utilities/textclip.ClipFont`). Mirror errors are swallowed — the panel must never be affected. `get_snapshot()` returns the copy; `/api/stream/status` renders it as round LEDs (`_preview_data_url`, cached per frame). PIL-based plugins should push frames with `canvas.SetImage(img)` (one C call) rather than 2048 `SetPixel`s — see `plugins/transit/render.push_pil_to_canvas`.

**Display page pinning.** `PluginManager.force_plugin(pid, pin=True)` pins a plugin (`_held`); the Dashboard calls it with `pin=False` (`POST /api/display/show/<pid>?pin=0`) to show a module once and continue the rotation from it. When pinned: `_dispatch` draws only it (ignoring duration, flight priority and clock-only) and the update loop keeps refreshing it even if disabled. `release_hold()` (`POST /api/display/return`) unpins; config saves call `reload_rotation()`, which keeps the pin.

The dashboard uses SSE (`/api/stream`) for live updates. The Alpine.js `init()` has a `_pollTimer` guard to prevent double-initialization.

### Adding a new plugin — checklist

Every new module must ship with all of these (the owner asked for this explicitly):
1. `plugins/<id>/manager.py` + guarded import and `pm.register()` block in `main.py`.
   - Override `enabled` to `self.config.get("enabled", False)`. `BasePlugin` defaults to True, and existing installs have no config block for a new module, so it would be polled in the background and show as Enabled on the config page without anyone turning it on.
2. A `plugins.<id>` block and `plugin_order` entry in `config/config.example.json`; any keys in `config/secrets.example.json` and the Secrets Schema below (and excluded from the generic "Other Secrets" card).
3. A hand-written config card in `config.html` (its Save also posts `pluginOrder` so existing installs pick it up), plus its display name in `window.PLUGIN_NAMES` in `web/templates/base.html` (the one list used by Dashboard, Config and Display).
4. **An About section in `web/templates/about.html`** — a collapsible card in `#aboutAccordion` (same markup as the others: icon, one-paragraph overview, then `subsection-title` blocks for colours/legends/sources) written for end users, not developers.
5. A section in this file describing the design and any gotchas.
6. No personal values (hostnames, usernames, keys) in examples or placeholders — the repo is public.
7. Works on a phone: no horizontal page scroll at 390 px wide (wrap wide tables in `.table-responsive`, give flex rows `min-width:0` on text and `flex-shrink-0` on controls). Friends configure their Pis from phones.

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
- **Alpine `x-show` vs Bootstrap display utilities** — `x-show` hides with inline `display:none`, which loses to Bootstrap's `d-flex`/`d-block`/etc. (`!important`). Never put `x-show` on an element that has a `d-*` display class: wrap it, or use `:class="{'d-none': …}"`.
- **Bluetooth audio** — the config/dashboard Bluetooth buttons use `bluetoothctl` directly (`/api/system/bt/*`). The legacy `POST /api/system/connect-bt` runs `~/bt-connect.sh` for the app's user if it exists (not in the repo — a per-Pi custom script), otherwise reconnects every paired device. Never hard-code `/home/<user>` paths; installs run under different usernames.
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
  "mta_api_key": "",
  "spotify_client_id": "",
  "spotify_refresh_token": "",
  "lastfm_api_key": ""
}
```

`cta_*` keys are required for Chicago; `mta_api_key` is optional (subway feeds currently work without one).

**Never commit API keys** — the GitHub repo is public. Keys live only in each device's gitignored `config/secrets.json`, entered through the web config page (the transit card has its own key fields; transit keys are hidden from the generic "Other Secrets" card). The owner reuses one pair of CTA keys across their own and friends' Pis, so if a key gets revoked or rate-limited, every one of those Pis is affected.

Per-calendar secrets are added dynamically: `calendar_url_<uid>` (ICS link) and `calendar_pw_<uid>` (CalDAV app-specific password), where `<uid>` is the source's id in `plugins.calendar.sources`.

`flightaware_reset_days` is no longer used — FlightAware always resets on the 1st. The field may exist in older installs but is ignored.
