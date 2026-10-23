"""
TransitPlugin — next three arrivals at a chosen stop, station-sign style.

Config (plugins.transit):
  agency        id from agencies.AGENCIES (e.g. "cta_train", "rtd", "mta_subway")
  routes        list of route ids to include (any number of lines at the stop)
  stop_id       stop/station id from the agency's data
  direction     "both" or a provider direction key
  min_minutes   hide arrivals sooner than this (walk time to the stop)
  dest_aliases  {"Full destination name": "Short"} user overrides

Arrival times are stored as absolute timestamps, so the minute countdown stays
accurate between fetches.
"""

import logging
import threading
import time
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.transit.agencies import AGENCIES, get_provider
from plugins.transit.providers.base import ProviderConfigError, ProviderNotReady
from plugins.transit.render import (
    MAX_LABEL_CHARS, build_rows, label_column_width, push_pil_to_canvas, render_board,
)

log = logging.getLogger(__name__)

STALE_AFTER_SECS = 300      # keep showing last good data this long when fetches fail
KEEP_ARRIVALS = 8           # extra arrivals held so rows refill as trains depart


class TransitPlugin(BasePlugin):
    PLUGIN_ID = "transit"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._arrivals: list = []
        self._fetched_at: float = 0.0
        self._ln_w: Optional[int] = None
        self._last_render_key: Optional[tuple] = None

    def reset(self):
        self._last_render_key = None

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        with self._lock:
            self._arrivals = []
            self._fetched_at = 0.0
        self._ln_w = None
        self._last_render_key = None

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _current(self) -> list:
        """Arrivals still in the future and past the walk-time cutoff."""
        now = time.time()
        cutoff = now + 60 * float(self.config.get("min_minutes", 0) or 0)
        with self._lock:
            if now - self._fetched_at > STALE_AFTER_SECS:
                return []
            return [a for a in self._arrivals
                    if a.arrival_ts >= cutoff and a.arrival_ts >= now - 30]

    def update(self):
        agency = self.config.get("agency", "")
        stop_id = str(self.config.get("stop_id", "") or "")
        routes = [str(r) for r in self.config.get("routes", []) or []]
        if not agency or not stop_id:
            return
        prov = get_provider(agency)
        if prov is None:
            log.warning(f"[transit] Unknown agency '{agency}'")
            return
        try:
            arrivals = prov.get_arrivals(stop_id, routes, self.config.get("direction", "both"))
        except ProviderNotReady as e:
            log.info(f"[transit] {agency} not ready: {e}")
            return
        except ProviderConfigError as e:
            log.warning(f"[transit] {agency}: {e}")
            return
        except Exception as e:
            log.warning(f"[transit] {agency} fetch failed: {e}")
            return

        ln_w = self._ln_w
        if ln_w is None:
            ln_w = self._label_width(prov, routes)

        with self._lock:
            self._arrivals = arrivals[:KEEP_ARRIVALS]
            self._fetched_at = time.time()
            self._ln_w = ln_w
        log.debug(f"[transit] {agency} stop {stop_id}: {len(arrivals)} arrivals")

    @staticmethod
    def _label_width(prov, routes: list) -> Optional[int]:
        """Ln column width from every configured line, so it doesn't jump between rows."""
        try:
            wanted = set(routes)
            w = 0
            for r in prov.list_routes():
                if r["id"] in wanted and not r.get("block"):
                    w = max(w, len(r["label"][:MAX_LABEL_CHARS]) * 4)
            return max(w, 8)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def has_content(self) -> bool:
        return bool(self._current())

    def draw(self) -> bool:
        arrivals = self._current()
        if not arrivals:
            return False
        spec = AGENCIES.get(self.config.get("agency", ""), {})
        ln_w = max(self._ln_w or 0, label_column_width(arrivals))
        ln_w, rows = build_rows(arrivals, alias_set=spec.get("alias_set", ""),
                                user_aliases=self.config.get("dest_aliases") or {}, ln_w=ln_w)
        key = (ln_w, tuple((r["label"], r["dest"], r["min"]) for r in rows))
        if key != self._last_render_key:
            push_pil_to_canvas(render_board(ln_w, rows), self.display_manager)
            self._last_render_key = key
        return True
