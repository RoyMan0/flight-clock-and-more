"""
TransitPlugin — upcoming arrivals at one or more stops, station-sign style.

Config (plugins.transit):
  stops             list of watched stops, each:
                      agency       id from agencies.AGENCIES ("cta_train", "rtd", …)
                      routes       route ids to include at this stop (one or more)
                      stop_id      stop/station id from the agency's data
                      stop_name    full name (for the UI)
                      label        optional short name for the stop header in cycle mode
                      direction    "both" or a provider direction key
                      min_minutes  walk time: arrivals sooner than this are shown grey
  display_mode      "combined" — next 3 across every stop, soonest first
                    "cycle"    — next 3 for each stop, one stop per page
  seconds_per_page  page time in cycle mode
  dest_aliases      {"Full destination name": "Short"} user overrides

Legacy single-stop configs (agency/routes/stop_id/direction at the top level)
are read as a one-item `stops` list.

Arrival times are stored as absolute timestamps, so the minute countdown stays
accurate between fetches.
"""

import logging
import threading
import time
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.transit.abbrev import shorten
from plugins.transit.agencies import AGENCIES, get_provider
from plugins.transit.providers.base import ProviderConfigError, ProviderNotReady
from plugins.transit.render import (
    HEADER_MAX_CHARS, MAX_LABEL_CHARS, ROW_Y, build_rows, label_column_width,
    push_pil_to_canvas, render_board,
)

log = logging.getLogger(__name__)

STALE_AFTER_SECS = 300      # keep showing a stop's last good data this long when fetches fail
KEEP_ARRIVALS = 8           # extra arrivals held so rows refill as trains depart
DEFAULT_PAGE_SECS = 6


# ------------------------------------------------------------------
# Shared helpers (also used by the web preview endpoint)
# ------------------------------------------------------------------

def configured_stops(config: dict) -> list[dict]:
    """The list of watched stops, upgrading a legacy single-stop config."""
    stops = config.get("stops")
    if stops is None and config.get("stop_id"):
        stops = [{k: config.get(k) for k in
                  ("agency", "routes", "stop_id", "stop_name", "direction", "min_minutes")}]
    return [s for s in (stops or []) if s.get("agency") and s.get("stop_id")]


def stop_key(stop: dict) -> tuple:
    return (stop.get("agency"), str(stop.get("stop_id")),
            tuple(str(r) for r in stop.get("routes") or []), stop.get("direction", "both"))


def fetch_stop(stop: dict) -> list:
    """Live arrivals for one watched stop (raises provider errors)."""
    agency = stop["agency"]
    prov = get_provider(agency)
    if prov is None:
        raise ProviderConfigError(f"Unknown agency '{agency}'")
    arrivals = prov.get_arrivals(str(stop["stop_id"]),
                                 [str(r) for r in stop.get("routes") or []],
                                 stop.get("direction", "both") or "both")
    alias_set = AGENCIES.get(agency, {}).get("alias_set", "")
    for a in arrivals:
        a.alias_set = alias_set
    return arrivals[:KEEP_ARRIVALS]


def line_label_width(stops: list[dict]) -> int:
    """Ln column width from every configured line, so it doesn't jump between rows/pages."""
    w = 8
    for stop in stops:
        prov = get_provider(stop.get("agency", ""))
        if prov is None:
            continue
        wanted = {str(r) for r in stop.get("routes") or []}
        try:
            for r in prov.list_routes():
                if r["id"] in wanted and not r.get("block"):
                    w = max(w, len(r["label"][:MAX_LABEL_CHARS]) * 4)
        except Exception:
            pass
    return w


def stop_title(stop: dict) -> str:
    if (stop.get("label") or "").strip():
        return stop["label"].strip()[:HEADER_MAX_CHARS]
    alias_set = AGENCIES.get(stop.get("agency", ""), {}).get("alias_set", "")
    return shorten(stop.get("stop_name") or str(stop.get("stop_id")), HEADER_MAX_CHARS, alias_set)


def build_pages(config: dict, results: list, ln_w: int = 8, now: float = None) -> list:
    """
    results: [(stop, [Arrival])] → pages: [(title or None, ln_w, rows)].
    Combined mode yields at most one page; cycle mode one page per stop with arrivals.
    """
    now = now or time.time()
    aliases = config.get("dest_aliases") or {}
    upcoming = []
    for stop, arrivals in results:
        # Walk time doesn't hide arrivals — ones you can't reach in time are drawn grey
        cutoff = now + 60 * float(stop.get("min_minutes", config.get("min_minutes", 0)) or 0)
        live = [a for a in arrivals if a.arrival_ts >= now - 30]
        for a in live:
            a.missable = a.arrival_ts < cutoff
        upcoming.append((stop, live))

    every = [a for _s, arr in upcoming for a in arr]
    ln_w = max(ln_w, label_column_width(every))
    n = len(ROW_Y)

    if config.get("display_mode", "combined") == "cycle":
        pages = []
        for stop, arr in upcoming:
            if arr:
                shown = _pick_rows(arr, n)
                pages.append((stop_title(stop), *build_rows(shown, now, user_aliases=aliases, ln_w=ln_w)))
        return pages

    if not every:
        return []
    return [(None, *build_rows(_pick_rows(every, n), now, user_aliases=aliases, ln_w=ln_w))]


def _pick_rows(arrivals: list, n: int) -> list:
    """Soonest n arrivals; if none of them can be reached in time, the last row shows the next one that can."""
    arrivals = sorted(arrivals, key=lambda a: a.arrival_ts)
    shown = arrivals[:n]
    if shown and all(a.missable for a in shown):
        catchable = next((a for a in arrivals[n:] if not a.missable), None)
        if catchable:
            shown = shown[:n - 1] + [catchable]
    return shown


# ------------------------------------------------------------------
# Plugin
# ------------------------------------------------------------------

class TransitPlugin(BasePlugin):
    PLUGIN_ID = "transit"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._results: dict = {}          # stop_key → (fetched_at, [Arrival])
        self._ln_w: Optional[int] = None
        self._page_idx = 0
        self._page_start = 0.0
        self._cycle_done = False
        self._last_render_key: Optional[tuple] = None

    def reset(self):
        self._page_idx = 0
        self._page_start = time.monotonic()
        self._cycle_done = False
        self._last_render_key = None

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        with self._lock:
            self._results = {}
        self._ln_w = None
        self.reset()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def update(self):
        stops = configured_stops(self.config)
        fresh = {}
        for stop in stops:
            key = stop_key(stop)
            try:
                fresh[key] = (time.time(), fetch_stop(stop))
            except ProviderNotReady as e:
                log.info(f"[transit] {stop['agency']} not ready: {e}")
            except ProviderConfigError as e:
                log.warning(f"[transit] {stop['agency']}: {e}")
            except Exception as e:
                log.warning(f"[transit] {stop['agency']} stop {stop['stop_id']} fetch failed: {e}")

        if self._ln_w is None and stops:
            self._ln_w = line_label_width(stops)

        with self._lock:
            # Keep the previous result for stops whose fetch failed this round
            keep = {stop_key(s) for s in stops}
            self._results = {k: v for k, v in self._results.items() if k in keep}
            self._results.update(fresh)
        log.debug(f"[transit] Updated {len(fresh)}/{len(stops)} stops")

    def _pages(self) -> list:
        now = time.time()
        stops = configured_stops(self.config)
        with self._lock:
            results = []
            for stop in stops:
                entry = self._results.get(stop_key(stop))
                if entry and now - entry[0] <= STALE_AFTER_SECS:
                    results.append((stop, list(entry[1])))
        return build_pages(self.config, results, self._ln_w or 8, now)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _page_secs(self) -> float:
        return float(self.config.get("seconds_per_page", DEFAULT_PAGE_SECS) or DEFAULT_PAGE_SECS)

    @property
    def display_duration(self) -> float:
        # In cycle mode, stay long enough to show every stop once
        base = float(self.config.get("display_duration", 30))
        if self.config.get("display_mode") == "cycle":
            n = len(configured_stops(self.config))
            return max(base, n * self._page_secs() + 1)
        return base

    def is_cycle_complete(self) -> bool:
        return self._cycle_done

    def has_content(self) -> bool:
        return bool(self._pages())

    def draw(self) -> bool:
        pages = self._pages()
        if not pages:
            return False

        if len(pages) > 1:
            now = time.monotonic()
            if now - self._page_start >= self._page_secs():
                self._page_idx += 1
                self._page_start = now
                if self._page_idx >= len(pages):
                    # Every stop shown once — hold the last page; reset() starts over
                    self._page_idx = len(pages) - 1
                    self._cycle_done = True
        idx = min(self._page_idx, len(pages) - 1)
        title, ln_w, rows = pages[idx]

        key = (idx, title, ln_w, tuple((r["label"], r["dest"], r["min"], r["missable"]) for r in rows))
        if key != self._last_render_key:
            push_pil_to_canvas(render_board(ln_w, rows, title), self.display_manager)
            self._last_render_key = key
        return True
