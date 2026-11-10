"""
SnowReportPlugin — snow conditions for any number of ski resorts worldwide,
from OnTheSnow's public resort pages (ots.py).

Config (plugins.snow_report):
  resorts          ordered [{id, name, label}] — id "com/colorado/keystone" (resorts.json);
                   label is an optional short name
  display_mode     "cards" (one resort per card) or "rows" (table like transit)
  transition       cards: "scroll" (slide up) or "static" (cut)
  seconds_per_resort  cards: time per resort (split across its pages)
  metrics          {new, base, runs, lifts, temp, forecast}: shown on cards
  row_columns      rows: up to 3 of new/base/runs/lifts
  new_window       "24" / "48" / "72" hours of new snow
  hide_closed      skip resorts that aren't open
  scroll_speed     rows: px/frame when there are more than 3 resorts
  morning_only     only show 6–10 am
  update_interval  seconds between refreshes (default 3600 — one page per resort per hour)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.snow_report import ots
from plugins.snow_report import render as R
from plugins.transit.render import push_pil_to_canvas
from utilities.paths import APP_ROOT, DATA_ROOT

log = logging.getLogger(__name__)

RESORTS_FILE = os.path.join(APP_ROOT, "plugins", "snow_report", "resorts.json")
CACHE_FILE = os.path.join(DATA_ROOT, "data", "snow_report", "reports.json")
FETCH_GAP = 1.0                  # seconds between resort pages (be polite)
MIN_PAGE_SECS = 2.5
TRANSITION_PX = 2                # px/frame for slides
DEFAULT_METRICS = {"new": True, "base": True, "runs": True, "lifts": True, "temp": True, "forecast": True}
DEFAULT_ROW_COLUMNS = ["new", "runs"]

_resort_index = None


def resort_index() -> list:
    global _resort_index
    if _resort_index is None:
        try:
            with open(RESORTS_FILE) as f:
                _resort_index = json.load(f)
        except (OSError, json.JSONDecodeError):
            _resort_index = []
    return _resort_index


def search_resorts(q: str, limit: int = 40) -> list:
    words = [w for w in (q or "").lower().split() if w]
    if not words:
        return []
    out = []
    for r in resort_index():
        hay = f"{r['name']} {r.get('short', '')} {r['region']} {r['country']}".lower()
        if all(w in hay for w in words):
            starts = r["name"].lower().startswith(words[0]) or r.get("short", "").lower().startswith(words[0])
            out.append((0 if starts else 1, r["name"], r))
    out.sort(key=lambda t: (t[0], t[1]))
    return [r for _, _, r in out[:limit]]


def units() -> str:
    try:
        from core.config_manager import get_config
        return get_config().get("location", "units", default="imperial") or "imperial"
    except Exception:
        return "imperial"


def configured_resorts(config: dict) -> list:
    """Resorts with a current id ("<edition>/<region>/<slug>"); the old version's numeric ids are ignored."""
    return [r for r in config.get("resorts") or [] if isinstance(r.get("id"), str) and "/" in r["id"]]


def display_name(resort: dict, rep: Optional[dict]) -> str:
    return (resort.get("label") or "").strip() or (rep or {}).get("short") or resort.get("name") or resort.get("id", "?")


def metrics_of(config: dict) -> dict:
    return {**DEFAULT_METRICS, **(config.get("metrics") or {})}


def window_of(config: dict) -> str:
    w = str(config.get("new_window", "24"))
    return w if w in ("24", "48", "72") else "24"


class SnowReportPlugin(BasePlugin):
    PLUGIN_ID = "snow_report"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._reports: dict = {}          # id → report (+ "fetched": epoch) or {"error": …}
        self._load_cache()
        self._frame = 0
        self._seq = []                    # [(resort, rep, page)] for this turn (cards)
        self._pos = 0
        self._pos_start = 0
        self._prev_img = None
        self._trans = 0
        self._full_trans = False
        self._trans_x0 = 0
        self._cycle_done = False
        self._strip = None
        self._strip_key = None
        self._offset = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_cache(self):
        try:
            with open(CACHE_FILE) as f:
                self._reports = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._reports = {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._reports, f)
            os.replace(tmp, CACHE_FILE)
        except OSError as e:
            log.debug(f"[snow] couldn't save cache: {e}")

    @property
    def update_interval(self) -> float:
        return max(900.0, float(self.config.get("update_interval", 3600)))

    def update(self):
        ids = [r["id"] for r in configured_resorts(self.config)]
        fresh_for = self.update_interval - 60
        changed = False
        for rid in ids:
            with self._lock:
                old = self._reports.get(rid) or {}
            if old.get("fetched") and time.time() - old["fetched"] < fresh_for:
                continue                                   # still fresh (e.g. after a restart)
            try:
                rep = ots.fetch_report(rid)
                rep["fetched"] = time.time()
            except Exception as e:
                log.info(f"[snow] {rid}: {e}")
                if old and not old.get("error"):
                    continue                               # keep the last good report
                rep = {"id": rid, "error": str(e), "fetched": time.time() - fresh_for + 600}
            with self._lock:
                self._reports[rid] = rep
            changed = True
            time.sleep(FETCH_GAP)
        with self._lock:
            for rid in [k for k in self._reports if k not in ids]:
                del self._reports[rid]
                changed = True
            if changed:
                self._save_cache()

    def entries(self) -> list:
        """[(resort, report)] to show, in the user's order (hide_closed applied)."""
        with self._lock:
            reps = dict(self._reports)
        out = []
        for r in configured_resorts(self.config):
            rep = reps.get(r["id"])
            if not rep:
                continue
            if self.config.get("hide_closed") and not rep.get("error") and not rep.get("open"):
                continue
            out.append((r, rep))
        return out

    # ------------------------------------------------------------------
    # BasePlugin
    # ------------------------------------------------------------------

    def _in_hours(self) -> bool:
        return not self.config.get("morning_only") or 6 <= datetime.now().hour < 10

    def has_content(self) -> bool:
        return self._in_hours() and bool(self.entries())

    def _mode(self) -> str:
        return "rows" if self.config.get("display_mode") == "rows" else "cards"

    def _row_cols(self) -> list:
        cols = [c for c in (self.config.get("row_columns") or DEFAULT_ROW_COLUMNS) if c in R.ROW_COLS]
        return cols[:3] or ["new"]

    @property
    def display_duration(self) -> float:
        base = float(self.config.get("display_duration", 30))
        n = len(self.entries())
        if self._mode() == "cards":
            # One full pass: each page's share of seconds_per_resort (≥ MIN_PAGE_SECS) + slides
            seq = self._seq or self._build_seq()
            secs = sum(max(MIN_PAGE_SECS, self._spr() / s[4]) + R.MATRIX_H / TRANSITION_PX / 10 for s in seq)
            return max(base, secs)
        if n > 3:
            h = n * R.LINE_H + 2
            return max(base, h / (self._scroll_speed() * 10) + 2)
        return base

    def _spr(self) -> float:
        try:
            return max(3.0, float(self.config.get("seconds_per_resort", 8)))
        except (TypeError, ValueError):
            return 8.0

    def _scroll_speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("scroll_speed", 1)))
        except (TypeError, ValueError):
            return 1.0

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self._strip_key = None
        self.reset()

    def reset(self):
        self._seq, self._pos, self._pos_start = [], 0, self._frame
        self._prev_img, self._trans, self._cycle_done = None, 0, False
        self._offset = 0.0

    def is_cycle_complete(self) -> bool:
        return self._cycle_done

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------

    def _build_seq(self) -> list:
        m, w, u = metrics_of(self.config), window_of(self.config), units()
        seq = []
        for resort, rep in self.entries():
            name = display_name(resort, rep)
            if rep.get("error"):
                seq.append((resort, rep, ("error", None), name, 1))
                continue
            pages = R.card_pages(rep, name, m, w, u)
            for p in pages:
                seq.append((resort, rep, p, name, len(pages)))
        return seq

    def _render_seq(self, i: int):
        resort, rep, page, name, _n = self._seq[i]
        if page[0] == "error":
            return R.render_error(name, rep.get("error") or "No data")
        return R.render_page(rep, name, page, window_of(self.config), units(),
                             metrics_of(self.config).get("temp", True), self._frame)

    def _draw_cards(self):
        if not self._seq:
            self._seq = self._build_seq()
            self._pos, self._pos_start = 0, self._frame
            if not self._seq:
                return None
        cur = self._render_seq(self._pos)
        if self._trans > 0 and self._prev_img is not None:
            body_h = R.MATRIX_H - R.BODY_Y
            total = R.MATRIX_H if self._full_trans else body_h
            off = total - self._trans
            img = (R.compose_full_scroll(self._prev_img, cur, off) if self._full_trans
                   else R.compose_body_scroll(self._prev_img, cur, off, self._trans_x0))
            self._trans = max(0, self._trans - TRANSITION_PX)
            return img
        # Time on this page: the resort's time split across its pages
        page_frames = int(max(MIN_PAGE_SECS, self._spr() / self._seq[self._pos][4]) * 10)
        if self._frame - self._pos_start >= page_frames:
            nxt = self._pos + 1
            if nxt >= len(self._seq):
                self._cycle_done = True
                nxt = 0
                self._seq = self._build_seq() or self._seq   # pick up fresh data each pass
            same_resort = self._seq[nxt][0] is self._seq[self._pos % len(self._seq)][0] and nxt != 0
            both_lines = same_resort and self._seq[nxt][2][0] == "lines" and self._seq[self._pos][2][0] == "lines"
            self._trans_x0 = R.RIGHT_X - 1 if both_lines else 0
            self._prev_img = cur
            self._pos, self._pos_start = nxt, self._frame
            if self.config.get("transition", "scroll") == "scroll" and len(self._seq) > 1:
                self._full_trans = not same_resort
                self._trans = R.MATRIX_H if self._full_trans else R.MATRIX_H - R.BODY_Y
            return self._draw_cards()
        return cur

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------

    def _draw_rows(self):
        ents = [(rep, display_name(resort, rep)) for resort, rep in self.entries()]
        if not ents:
            return None
        cols, w, u = self._row_cols(), window_of(self.config), units()
        if len(ents) <= 3:
            return R.render_rows_static(ents, cols, w, u)
        key = (tuple((id(rep), n) for rep, n in ents), tuple(cols), w, u)
        if key != self._strip_key:
            self._strip = R.build_rows_strip(ents, cols, w, u)
            self._strip_key = key
        img = R.render_rows_frame(self._strip, ents, cols, w, u, int(self._offset))
        self._offset += self._scroll_speed()
        if self._offset >= self._strip.height:
            self._offset = 0.0
            self._cycle_done = True
        return img

    # ------------------------------------------------------------------

    def describe(self):
        if not self._in_hours():
            return None
        ents = self.entries()
        if not ents:
            return None
        out = {"mode": self._mode(), "units": units(), "window": window_of(self.config),
               "resorts": [{"name": display_name(r, rep), **{k: rep.get(k) for k in (
                   "id", "open", "opening_date", "base_cm", "summit_cm", "new_cm", "runs_open", "runs_total",
                   "runs_pct", "lifts_open", "lifts_total", "weather", "forecast", "error")}} for r, rep in ents]}
        if self._mode() == "cards" and self._seq:
            out["index"] = next((i for i, (r, _) in enumerate(ents) if r is self._seq[self._pos][0]), 0)
            out["page"] = self._seq[self._pos][2][0]
        return out

    def draw(self) -> bool:
        if not self._in_hours():
            return False
        self._frame += 1
        img = self._draw_rows() if self._mode() == "rows" else self._draw_cards()
        if img is None:
            return False
        push_pil_to_canvas(img, self.display_manager)
        return True
