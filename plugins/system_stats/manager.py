"""
SystemStatsPlugin — CPU / GPU / memory (plus optional uptime, disk and network
I/O) for this Pi and any number of other machines reached over SSH.

Config (plugins.system_stats):
  connections   ordered list; each:
                  name       optional label (default: the machine's hostname)
                  type       "local" (this Pi) or "ssh"
                  host, user, port   for ssh
                  enabled    shown or not
                  metrics    {"cpu","gpu","mem","uptime","disk","net": bool}
                  disk_max, net_max  full-scale bytes/s for the log-scale I/O bars
  scroll_speed  px/frame when everything doesn't fit on one screen

One machine with up to 3 stats is a static board; more machines or stats
scroll bottom-to-top like Breaking News.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.system_stats.collect import Collector
from plugins.system_stats.render import (
    build_sections, build_strip, needs_scroll, render_static, render_strip_frame,
)
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

DEFAULT_SCROLL_SPEED = 1.0
DEFAULT_CONNECTION = {"name": "", "type": "local", "enabled": True,
                      "metrics": {"cpu": True, "gpu": True, "mem": True}}
_collectors: dict = {}            # connection key → Collector (shared with the Test endpoint)
_collectors_lock = threading.Lock()


def conn_key(conn: dict) -> tuple:
    if (conn.get("type") or "local") == "local":
        return ("local",)
    return ("ssh", (conn.get("host") or "").strip().lower(), (conn.get("user") or "").strip(),
            int(conn.get("port") or 22))


def collector_for(conn: dict) -> Collector:
    key = conn_key(conn)
    with _collectors_lock:
        c = _collectors.get(key)
        if c is None:
            c = _collectors[key] = Collector(conn)
        return c


def enabled_connections(config: dict) -> list:
    conns = config.get("connections")
    if conns is None:
        conns = [DEFAULT_CONNECTION]
    return [c for c in conns if c.get("enabled", True)]


def units() -> str:
    try:
        from core.config_manager import get_config
        return get_config().get("location", "units", default="imperial") or "imperial"
    except Exception:
        return "imperial"


class SystemStatsPlugin(BasePlugin):
    PLUGIN_ID = "system_stats"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._results: dict = {}          # conn_key → (stats | None, error | None)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sysstats")
        self._in_flight: set = set()      # conn keys with a poll still running
        self._y_offset = 0.0
        self._strip = None
        self._key: Optional[tuple] = None

    @property
    def enabled(self) -> bool:
        # Off unless configured: installs from before this module have no config
        # block, and the update loop polls every enabled plugin even when it isn't
        # in plugin_order.
        return bool(self.config.get("enabled", False))

    def reset(self):
        self._y_offset = 0.0
        self._key = None

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self._strip = None
        self.reset()

    @property
    def update_interval(self) -> float:
        return float(self.config.get("update_interval", 5))

    def update(self):
        """Start a background poll per machine and return immediately — the plugin
        manager runs every plugin's update() on one thread, so a slow or hung
        machine must not hold it up. Each poll stores its own result when done."""
        conns = enabled_connections(self.config)
        live = {conn_key(c) for c in conns}
        with self._lock:
            self._results = {k: v for k, v in self._results.items() if k in live}

        def poll(conn, key):
            try:
                result = (collector_for(conn).sample(), None)
            except Exception as e:
                log.debug(f"[system_stats] {conn.get('host') or 'local'}: {e}")
                result = (None, str(e))
            with self._lock:
                self._in_flight.discard(key)
                if key in {conn_key(c) for c in enabled_connections(self.config)}:
                    self._results[key] = result

        for conn in conns:
            key = conn_key(conn)
            with self._lock:
                if key in self._in_flight:
                    continue                  # previous poll of this machine still running
                self._in_flight.add(key)
            self._pool.submit(poll, conn, key)

    def has_content(self) -> bool:
        return bool(enabled_connections(self.config))

    def _scroll_speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("scroll_speed", DEFAULT_SCROLL_SPEED)))
        except (TypeError, ValueError):
            return DEFAULT_SCROLL_SPEED

    @property
    def display_duration(self) -> float:
        base = float(self.config.get("display_duration", 30))
        if self._strip is not None:
            return max(base, self._strip.height / (self._scroll_speed() * 10) + 1)
        return base

    def _rows(self) -> list:
        with self._lock:
            results = dict(self._results)
        rows = []
        for conn in enabled_connections(self.config):
            if conn_key(conn) in results:
                stats, error = results[conn_key(conn)]
                rows.append((conn, stats, error))
        return rows

    def describe(self):
        from plugins.system_stats import render as r
        rows = self._rows()
        if not rows:
            return None
        u = units()
        hosts = []
        for conn, stats, error in rows:
            name = (conn.get("name") or "").strip() or (stats or {}).get("host") or conn.get("host") or "This Pi"
            if error or stats is None:
                hosts.append({"name": name, "offline": True, "stats": []})
                continue
            avail = r.available_stats(conn, stats)
            items = []
            for key in (k for k in avail if k != "uptime"):
                label, colour = r.STATS[key]
                item = {"key": key, "label": label, "color": list(colour)}
                if key in ("cpu", "gpu", "mem"):
                    pct = stats.get(f"{key}_pct")
                    temp = stats.get(f"{key}_temp") if key != "mem" else None
                    item.update(value=None if pct is None else f"{round(pct)}%", frac=(pct or 0) / 100.0)
                    if temp is not None:
                        item.update(temp=r.fmt_temp(temp, u), temp_color=list(r.temp_colour(temp)))
                else:
                    a, b = ("disk_r", "disk_w") if key == "disk" else ("net_rx", "net_tx")
                    full = float(conn.get("disk_max" if key == "disk" else "net_max")
                                 or (7e9 if key == "disk" else 1.25e9))
                    total = None if stats.get(a) is None else (stats.get(a) or 0) + (stats.get(b) or 0)
                    item.update(a=r.fmt_rate(stats.get(a)), b=r.fmt_rate(stats.get(b)),
                                a_mark="R" if key == "disk" else "↓", b_mark="W" if key == "disk" else "↑",
                                frac=r.log_frac(total, full))
                items.append(item)
            hosts.append({"name": name, "offline": False, "stats": items,
                          "uptime": r.fmt_uptime(stats.get("uptime_s")) if "uptime" in avail else ""})
        return {"hosts": hosts}

    def draw(self) -> bool:
        rows = self._rows()
        if not rows:
            return False                       # nothing polled yet
        # Results only change when update() stores a new poll, so key on those objects
        key = (units(),) + tuple((id(st), e) for _c, st, e in rows)
        if key == self._key and self._strip is None:
            return True                        # static board already on the panel
        sections = build_sections(rows, units())
        if not needs_scroll(sections):
            self._strip = None
            if key != self._key:
                push_pil_to_canvas(render_static(sections), self.display_manager)
                self._key = key
            return True
        if key != self._key or self._strip is None:
            self._strip = build_strip(sections)
            self._key = key
        push_pil_to_canvas(render_strip_frame(self._strip, int(self._y_offset)), self.display_manager)
        self._y_offset = (self._y_offset + self._scroll_speed()) % self._strip.height
        return True
