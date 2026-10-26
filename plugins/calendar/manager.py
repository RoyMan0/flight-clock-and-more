"""
CalendarPlugin — upcoming events from ICS links and CalDAV accounts.

Config (plugins.calendar):
  sources           [{uid, type: "ics"|"caldav", name, color, enabled,
                      server_url, username, calendars: [{href, name, color, enabled}]}]
                    The ICS link / app-specific password live in secrets.json as
                    calendar_url_<uid> / calendar_pw_<uid>.
  display_mode      "detailed" (one event at a time) | "compact" (scrolling list)
  days_ahead        1 = today only, 2 = today + tomorrow, …
  max_events        cap on events kept (compact shows them all)
  events_per_visit  detailed: events shown per rotation turn
  seconds_per_event detailed: hold time per event
  scroll_speed      compact: px/frame when the list doesn't fit
  reminders         interrupt the rotation when an event is about to start
  reminder_minutes  how far ahead

Reminders are a live-priority takeover that clears itself: has_live_content()
turns True when an event is due, the card shows for REMINDER_SECONDS, then the
event is marked as reminded.
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.calendar import render as R
from plugins.calendar import sources
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

TRANSITION_PX = 2               # detailed: px per frame when sliding to the next event
REMINDER_SECONDS = 15
FRAME_RATE = 10


def secret_key(src: dict) -> str:
    kind = "pw" if (src.get("type") or "ics") == "caldav" else "url"
    return f"calendar_{kind}_{src.get('uid', '')}"


def get_secret(key: str) -> str:
    try:
        from core.config_manager import get_config
        return get_config().get_secret(key, "") or ""
    except Exception:
        return ""


def clock_format() -> str:
    try:
        from core.config_manager import get_config
        return get_config().get("location", "clock_format", default="12hr") or "12hr"
    except Exception:
        return "12hr"


def _sort_key(ev):
    s = ev["start"]
    if isinstance(s, datetime):
        return (s.date(), 1, s.timestamp())
    return (s, 0, 0)                  # all-day first within its day


def upcoming(events: list, days_ahead: int, max_events: int, now=None) -> list:
    """Events not yet over that start inside the window, soonest first."""
    now = now or datetime.now().astimezone()
    today = now.date()
    _start, end = sources.window(days_ahead)
    keep = []
    for ev in events:
        if ev["all_day"]:
            if ev["end"] <= today or ev["start"] >= end.date():
                continue
        elif ev["end"] <= now or ev["start"] >= end:
            continue
        keep.append(ev)
    # The same event from two sources (e.g. a shared calendar) shows once
    seen, out = set(), []
    for ev in sorted(keep, key=_sort_key):
        k = (ev["uid"], str(ev["start"]), ev["title"])
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out[:max(1, int(max_events))]


def fetch_all(config: dict, secrets_override: Optional[dict] = None) -> tuple:
    """({uid: events}, {uid: error}) for every enabled source (used by Test)."""
    start, end = sources.window(int(config.get("days_ahead", 3)))
    results, errors = {}, {}
    for src in config.get("sources") or []:
        if not src.get("enabled", True):
            continue
        key = secret_key(src)
        secret = (secrets_override or {}).get(key) or get_secret(key)
        try:
            results[src["uid"]] = sources.fetch_source(src, secret, start, end)
        except Exception as e:
            errors[src["uid"]] = str(e)
    return results, errors


class CalendarPlugin(BasePlugin):
    PLUGIN_ID = "calendar"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._by_source: dict = {}         # uid → events (last good fetch)
        self._errors: dict = {}            # uid → error text
        self._in_flight: set = set()
        self._failing: set = set()
        self._version = 0                  # bumps when events change
        # display state
        self._start = time.monotonic()
        self._visit: list = []             # detailed: events for this turn
        self._panels: dict = {}            # (version, idx, clock, minute) → panel image
        self._strip = None
        self._strip_key = None
        self._y_offset = 0.0
        self._key = None
        # reminders
        self._priority = False
        self._reminded: dict = {}          # event key → reminded at (epoch)
        self._reminder_ev = None
        self._reminder_start = 0.0
        self._due_cache = (0.0, None)      # (checked at, event) — has_live_content runs every frame

    @property
    def enabled(self) -> bool:
        # Off unless configured: existing installs have no calendar block, and the
        # update loop polls every enabled plugin even when it isn't in plugin_order.
        return bool(self.config.get("enabled", False))

    @property
    def update_interval(self) -> float:
        return max(300.0, float(self.config.get("update_interval", 900)))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def update(self):
        for src in self.config.get("sources") or []:
            uid = src.get("uid")
            if not uid or not src.get("enabled", True):
                continue
            with self._lock:
                if uid in self._in_flight:
                    continue
                self._in_flight.add(uid)
            threading.Thread(target=self._poll, args=(dict(src),), daemon=True,
                             name=f"calendar-{uid}").start()

    def _poll(self, src: dict):
        uid = src["uid"]
        start, end = sources.window(int(self.config.get("days_ahead", 3)))
        try:
            events = sources.fetch_source(src, get_secret(secret_key(src)), start, end)
            with self._lock:
                self._by_source[uid] = events
                self._errors.pop(uid, None)
                self._version += 1
            if uid in self._failing:
                log.info(f"[calendar] {src.get('name') or uid}: fetch recovered")
                self._failing.discard(uid)
        except Exception as e:
            with self._lock:
                self._errors[uid] = str(e)
            if uid not in self._failing:
                log.warning(f"[calendar] {src.get('name') or uid}: {e}")
                self._failing.add(uid)
        finally:
            with self._lock:
                self._in_flight.discard(uid)

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        live = {s.get("uid") for s in new_config.get("sources") or [] if s.get("enabled", True)}
        with self._lock:
            self._by_source = {k: v for k, v in self._by_source.items() if k in live}
            self._version += 1
        self.reset()
        if self.enabled:
            self.update()                     # show new/changed calendars without waiting 15 min

    def _events(self, now=None) -> list:
        with self._lock:
            merged = [ev for evs in self._by_source.values() for ev in evs]
        return upcoming(merged, int(self.config.get("days_ahead", 3)),
                        int(self.config.get("max_events", 20)), now)

    # ------------------------------------------------------------------
    # Reminders (live priority)
    # ------------------------------------------------------------------

    @staticmethod
    def _ev_key(ev) -> str:
        return f"{ev['uid']}|{ev['start']}"

    def _due_reminder(self, now=None):
        now = now or datetime.now().astimezone()
        ahead = float(self.config.get("reminder_minutes", 10)) * 60
        for ev in self._events(now):
            if ev["all_day"] or self._ev_key(ev) in self._reminded:
                continue
            until = (ev["start"] - now).total_seconds()
            if -60 <= until <= ahead:
                return ev
        return None

    def has_live_priority(self) -> bool:
        return bool(self.config.get("reminders", False))

    def _due_cached(self):
        checked, ev = self._due_cache
        if time.monotonic() - checked >= 1.0:
            ev = self._due_reminder()
            self._due_cache = (time.monotonic(), ev)
        return ev

    def has_live_content(self) -> bool:
        if self._reminder_ev is not None:
            return True                       # card on screen until its time is up
        return self._due_cached() is not None

    def on_priority_view(self, active: bool):
        self._priority = active

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def _mode(self) -> str:
        return "compact" if self.config.get("display_mode") == "compact" else "detailed"

    def _hold(self) -> float:
        try:
            return max(2.0, float(self.config.get("seconds_per_event", 6)))
        except (TypeError, ValueError):
            return 6.0

    def _transition(self) -> float:
        return R.PANEL_H / TRANSITION_PX / FRAME_RATE

    def _scroll_speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("scroll_speed", 1)))
        except (TypeError, ValueError):
            return 1.0

    def has_content(self) -> bool:
        return bool(self._events())

    def reset(self):
        self._start = time.monotonic()
        self._key = None
        self._y_offset = 0.0
        if self._priority:
            self._reminder_ev = self._due_reminder()
            self._reminder_start = time.monotonic()
        else:
            n = max(1, int(self.config.get("events_per_visit", 5)))
            self._visit = self._events()[:n]

    @property
    def display_duration(self) -> float:
        if self._mode() == "detailed":
            n = max(1, len(self._visit))
            return n * self._hold() + (n - 1) * self._transition()
        base = float(self.config.get("display_duration", 20))
        if self._strip is not None:
            return max(base, self._strip.height / (self._scroll_speed() * FRAME_RATE) + 1)
        return base

    def is_cycle_complete(self) -> bool:
        return False                          # display_duration covers one pass

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _panel(self, idx: int, ev, fmt: str):
        minute = int(time.time() // 60)       # NOW / day labels change with the clock
        key = (self._version, idx, fmt, minute)
        if key not in self._panels:
            if len(self._panels) > 40:
                self._panels.clear()
            self._panels[key] = R.render_panel(ev, fmt)
        return self._panels[key]

    def _push(self, key, make):
        if key != self._key:
            push_pil_to_canvas(make(), self.display_manager)
            self._key = key
        return True

    def draw(self) -> bool:
        fmt = clock_format()
        if self._priority and self._reminder_ev is None:
            self._reminder_ev = self._due_reminder()     # became due while already showing
            self._reminder_start = time.monotonic()
        if self._priority and self._reminder_ev is not None:
            return self._draw_reminder(fmt)
        if self._mode() == "compact":
            return self._draw_compact(fmt)
        return self._draw_detailed(fmt)

    def _draw_reminder(self, fmt: str) -> bool:
        ev = self._reminder_ev
        if time.monotonic() - self._reminder_start >= REMINDER_SECONDS:
            self._reminded[self._ev_key(ev)] = time.time()
            cutoff = time.time() - 2 * 86400
            self._reminded = {k: t for k, t in self._reminded.items() if t > cutoff}
            self._reminder_ev = None          # has_live_content() → False; rotation resumes
            self._due_cache = (0.0, None)
            return True
        title = R.reminder_title(ev)
        return self._push(("remind", title, int(time.time() // 60)),
                          lambda: R.render_detailed(ev, [R.render_panel(ev, fmt)],
                                                    header_title=title, header_colour=R.COL_REMIND))

    def _draw_detailed(self, fmt: str) -> bool:
        visit = self._visit or self._events()[:max(1, int(self.config.get("events_per_visit", 5)))]
        if not visit:
            return False
        self._visit = visit
        hold, trans = self._hold(), self._transition()
        elapsed = time.monotonic() - self._start
        idx = min(int(elapsed // (hold + trans)), len(visit) - 1)
        t = elapsed - idx * (hold + trans)
        offset = 0
        if idx < len(visit) - 1 and t > hold:
            offset = min(R.PANEL_H, int((t - hold) * FRAME_RATE * TRANSITION_PX))
        cur = self._panel(idx, visit[idx], fmt)
        if offset:
            nxt = self._panel(idx + 1, visit[idx + 1], fmt)
            header_ev = visit[idx + 1] if offset >= R.PANEL_H // 2 else visit[idx]
            return self._push(("d", self._version, idx, offset, header_ev["source"], fmt),
                              lambda: R.render_detailed(header_ev, [cur, nxt], offset))
        return self._push(("d", self._version, idx, 0, int(time.time() // 60), fmt),
                          lambda: R.render_detailed(visit[idx], [cur]))

    def _draw_compact(self, fmt: str) -> bool:
        events = self._events()
        if not events:
            return False
        rows = R.compact_rows(events)
        minute = int(time.time() // 60)
        if R.compact_fits(rows):
            self._strip = None
            return self._push(("c", self._version, minute, fmt),
                              lambda: R.render_compact_static(events, rows, fmt))
        skey = (self._version, minute, fmt)
        if self._strip is None or self._strip_key != skey:
            self._strip = R.build_compact_strip(rows, fmt)
            self._strip_key = skey
        push_pil_to_canvas(R.render_compact_frame(events, self._strip, int(self._y_offset)),
                           self.display_manager)
        self._key = None
        self._y_offset = (self._y_offset + self._scroll_speed()) % self._strip.height
        return True
