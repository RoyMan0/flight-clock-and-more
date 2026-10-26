"""
PluginManager — the main loop that drives all plugins.

Dispatch order (checked every frame):
  1. A plugin pinned from the web Display page
  2. Clock-only mode (clock_weather only)
  3. Live priority, "takeover" mode: the first plugin (flight tracker first,
     then plugin_order) whose has_live_priority() and has_live_content() are
     both True preempts the rotation for as long as both stay True. "Alerts"
     (a sports score change, a calendar reminder) are takeovers whose plugin
     clears has_live_content() itself after one pass.
  4. Normal rotation through enabled plugins in plugin_order. A live plugin in
     "interleave" mode is slotted in after every normal module.

Plugins shown as a priority (takeover or interleave) get
on_priority_view(True) before reset(), so they can show a priority-only view.

Background thread calls update() on each plugin at its configured interval.
"""

import sys
import time
import threading
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


class PluginManager:
    FPS = 10                     # Frames per second for the main loop
    FRAME_PERIOD = 1.0 / FPS

    def __init__(self, display_manager, config_manager):
        self.display = display_manager
        self.cfg = config_manager

        self._plugins: dict = {}          # plugin_id → plugin instance
        self._flight_plugin = None        # reference to FlightTrackerPlugin
        self._rotation: list = []         # ordered list of plugin_ids for cycling
        self._active_id: Optional[str] = None
        self._active_start: float = 0.0
        self._rotation_index: int = 0
        self._held: Optional[str] = None   # plugin pinned from the web Display page
        self._priority_id: Optional[str] = None      # plugin currently shown as a takeover
        self._interleaving: Optional[str] = None     # interleave plugin shown between normal modules
        self._lock = threading.Lock()

        # Background update thread
        self._update_last: dict = {}      # plugin_id → last update timestamp
        self._stop_event = threading.Event()
        self._update_thread = threading.Thread(
            target=self._update_loop, daemon=True, name="plugin-updater"
        )

    # ------------------------------------------------------------------
    # Plugin registration
    # ------------------------------------------------------------------

    def register(self, plugin_id: str, plugin):
        """Register a plugin instance. Call before run()."""
        self._plugins[plugin_id] = plugin
        self._update_last[plugin_id] = 0.0
        self.cfg.register_plugin_change(plugin_id, plugin.on_config_change)
        log.info(f"[plugins] Registered: {plugin_id}")

    def set_flight_plugin(self, plugin):
        """The flight tracker is checked first for live priority."""
        self._flight_plugin = plugin

    def _priority_order(self) -> list:
        """Flight tracker first, then plugin_order, then any other registered plugin."""
        order = []
        for pid, p in self._plugins.items():
            if p is self._flight_plugin:
                order.append(pid)
        order += [pid for pid in self._rotation if pid not in order]
        order += [pid for pid in self._plugins if pid not in order]
        return order

    def _find_live(self, mode: str, exclude: Optional[str] = None) -> Optional[str]:
        """First enabled plugin that is live right now in the given priority mode."""
        for pid in self._priority_order():
            if pid == exclude:
                continue
            p = self._plugins[pid]
            try:
                if (p.enabled and p.has_live_priority() and p.has_live_content()
                        and p.live_priority_mode() == mode):
                    return pid
            except Exception as e:
                log.debug(f"[plugins] Live check error ({pid}): {e}")
        return None

    def build_rotation(self):
        """Build the active rotation from config plugin_order + enabled flags."""
        order = self.cfg.get("plugin_order") or []
        self._rotation = [
            pid for pid in order
            if pid in self._plugins and self._plugins[pid].enabled
        ]
        log.info(f"[plugins] Rotation: {self._rotation}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        self._update_thread.start()
        self.build_rotation()

        if not self._rotation:
            log.error("[plugins] No plugins in rotation. Check config plugin_order and enabled flags.")
            return

        self._switch_to(self._rotation[0])

        log.info("[plugins] Starting main loop")
        try:
            while not self._stop_event.is_set():
                frame_start = time.monotonic()

                self._apply_brightness()
                try:
                    self._dispatch()
                except Exception as e:
                    # A bug in one plugin must never take the whole display down
                    # (systemd would restart the service and re-fetch every API).
                    log.exception(f"[plugins] Frame error ({self._active_id}): {e}")

                elapsed = time.monotonic() - frame_start
                sleep_time = self.FRAME_PERIOD - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nExiting")
            sys.exit(0)

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Frame dispatch
    # ------------------------------------------------------------------

    def _is_clock_only(self) -> bool:
        """Return True if clock-only mode is active right now."""
        disp = self.cfg.get("display") or {}
        if not disp.get("clock_only", False):
            return False
        start = disp.get("clock_only_start", "22:00")
        end   = disp.get("clock_only_end",   "06:00")
        now   = datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def _dispatch(self):
        # --- Pinned from the Display page: stay on it until "Return to Rotation" ---
        if self._held:
            if self._active_id != self._held:
                self._switch_to(self._held)
            held = self._plugins.get(self._held)
            try:
                if held is not None and held.draw():
                    self.display.swap()
            except Exception as e:
                log.error(f"[plugins] Draw error ({self._held}): {e}")
            return

        # --- Clock-only mode: show clock_weather only, no API calls ---
        if self._is_clock_only():
            if self._active_id != "clock_weather":
                log.info("[plugins] Clock-only mode active — switching to clock_weather")
                self._switch_to("clock_weather")
            cw = self._plugins.get("clock_weather")
            if cw:
                try:
                    if cw.draw():
                        self.display.swap()
                except Exception as e:
                    log.error(f"[plugins] Draw error (clock_weather): {e}")
            return

        # --- Live priority takeover (flight tracker first, then plugin_order) ---
        live_id = self._find_live("takeover")
        if live_id:
            if self._active_id != live_id or self._priority_id != live_id:
                self._priority_id = live_id
                self._interleaving = None
                self._switch_to(live_id, priority=True)
            try:
                if self._plugins[live_id].draw():
                    self.display.swap()
            except Exception as e:
                # Unprotected, this crashed the whole app (systemd restart) on any draw error
                log.error(f"[plugins] Draw error ({live_id} priority): {e}")
            return

        # --- Priority over: resume the rotation with the module it interrupted ---
        if self._priority_id:
            ended = self._priority_id
            self._priority_id = None
            if not self._rotation:
                return
            candidate_id = self._rotation[self._rotation_index % len(self._rotation)]
            candidate = self._plugins.get(candidate_id)
            if candidate is not None and not candidate.has_content():
                self._advance_rotation()
            else:
                self._switch_to(candidate_id)
            # If _advance_rotation couldn't find any plugin with content, the
            # ended priority plugin is still active. Don't draw it from the
            # normal path — just wait for the next frame.
            if self._active_id == ended and candidate_id != ended:
                return

        # --- Check if it's time to advance rotation ---
        active = self._active_plugin()
        if active is None:
            self._advance_rotation()
            return

        now = time.monotonic()
        duration_expired = (now - self._active_start) >= active.display_duration
        cycle_done = active.is_cycle_complete()

        if duration_expired or cycle_done:
            self._advance_rotation()
            active = self._active_plugin()
            if active is None:
                return

        # --- Draw current plugin ---
        drew = True
        try:
            drew = active.draw()
        except Exception as e:
            log.error(f"[plugins] Draw error ({self._active_id}): {e}")

        if not drew:
            log.info(f"[plugins] {self._active_id}.draw() returned False — advancing")
            self._advance_rotation()
            return  # skip swap; next frame the new plugin will draw

        self.display.swap()

    def _active_plugin(self):
        return self._plugins.get(self._active_id)

    def _switch_to(self, plugin_id: str, priority: bool = False):
        if plugin_id not in self._plugins:
            return
        old = self._active_id
        self._active_id = plugin_id
        self._active_start = time.monotonic()
        if old != plugin_id:
            self.display.clear()
            log.debug(f"[plugins] Switched: {old} → {plugin_id}{' (priority)' if priority else ''}")
        plugin = self._plugins[plugin_id]
        try:
            plugin.on_priority_view(priority)
            plugin.reset()
        except Exception as e:
            log.error(f"[plugins] Reset error ({plugin_id}): {e}")

    def _advance_rotation(self):
        if not self._rotation:
            return
        # A live "interleave" plugin goes between normal modules, without
        # moving the rotation index; the next advance resumes the rotation.
        if self._interleaving is None:
            il = self._find_live("interleave", exclude=self._active_id)
            if il:
                self._interleaving = il
                self._switch_to(il, priority=True)
                return
        just_shown, self._interleaving = self._interleaving, None
        for _ in range(len(self._rotation)):
            self._rotation_index = (self._rotation_index + 1) % len(self._rotation)
            candidate_id = self._rotation[self._rotation_index]
            candidate = self._plugins.get(candidate_id)
            if candidate_id == just_shown and len(self._rotation) > 1:
                continue                        # its own turn right after being interleaved
            if candidate is None or candidate.has_content():
                self._switch_to(candidate_id)
                return
            log.info(f"[plugins] Skipping {candidate_id} — has_content() returned False")
        # All plugins skipped — stay on current without switching
        log.warning("[plugins] All plugins skipped (no content); staying on current")

    # ------------------------------------------------------------------
    # Brightness schedule
    # ------------------------------------------------------------------

    def _apply_brightness(self):
        disp_cfg = self.cfg.get("display") or {}
        if not disp_cfg.get("night_brightness", True):
            return

        night_start = disp_cfg.get("night_start", "18:00")
        night_end = disp_cfg.get("night_end", "07:00")
        day_brightness = disp_cfg.get("brightness", 100)
        night_brightness = disp_cfg.get("brightness_night", 50)

        try:
            now = datetime.now().strftime("%H:%M")
            # Night period spans midnight: start=18:00, end=07:00
            # So night is 18:00–23:59 and 00:00–06:59
            if night_start <= night_end:
                is_night = night_start <= now < night_end
            else:
                is_night = now >= night_start or now < night_end

            target = night_brightness if is_night else day_brightness
            self.display.set_brightness(target)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Background update loop
    # ------------------------------------------------------------------

    def _update_loop(self):
        while not self._stop_event.is_set():
            now = time.monotonic()
            clock_only = self._is_clock_only()
            for pid, plugin in list(self._plugins.items()):
                held = pid == self._held        # a pinned plugin always gets fresh data
                if not plugin.enabled and not held:
                    continue
                if clock_only and pid != "clock_weather" and not held:
                    continue  # suppress all API calls during clock-only period
                last = self._update_last.get(pid, 0)
                if now - last >= plugin.update_interval:
                    try:
                        plugin.update()
                    except Exception as e:
                        log.error(f"[plugins] Update error ({pid}): {e}")
                    self._update_last[pid] = now
            time.sleep(1)

    # ------------------------------------------------------------------
    # On-demand control (web dashboard)
    # ------------------------------------------------------------------

    def force_plugin(self, plugin_id: str, pin: bool = True) -> bool:
        """Show a plugin now. pin=True (Display page) keeps it on screen until
        release_hold(); pin=False (Dashboard) shows it once and the rotation
        continues from there. Returns False if unknown."""
        if plugin_id not in self._plugins:
            return False
        with self._lock:
            self._update_last[plugin_id] = 0.0      # fetch fresh data right away
            self._interleaving = self._priority_id = None
            if pin:
                self._held = plugin_id
            else:
                self._held = None
                if plugin_id in self._rotation:
                    self._rotation_index = self._rotation.index(plugin_id)
            self._switch_to(plugin_id)
        return True

    def release_hold(self):
        """Unpin and resume normal rotation from the start."""
        self._held = None
        self.reload_rotation()

    def reload_rotation(self):
        """Re-read plugin_order from config and rebuild rotation."""
        if self._held:
            self.build_rotation()      # config saved while pinned: keep showing the pinned plugin
            return
        self.build_rotation()
        self._interleaving = self._priority_id = None
        if self._rotation:
            self._rotation_index = 0
            self._switch_to(self._rotation[0])

    def get_status(self) -> dict:
        held = self._plugins.get(self._held) if self._held else None
        try:
            held_has_content = bool(held.has_content()) if held else None
        except Exception:
            held_has_content = None
        return {
            "active_plugin": self._active_id,
            "held": self._held,
            "held_has_content": held_has_content,
            "priority_id": self._priority_id or self._interleaving,
            "rotation": self._rotation,
            "rotation_index": self._rotation_index,
            "active_since": self._active_start,
            "plugins": {
                pid: {"enabled": p.enabled, "has_live": p.has_live_content()}
                for pid, p in self._plugins.items()
            },
        }
