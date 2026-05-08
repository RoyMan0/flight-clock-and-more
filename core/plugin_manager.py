"""
PluginManager — the main loop that drives all plugins.

Priority dispatch (checked every frame):
  1. FlightTrackerPlugin (has_live_priority + has_live_content)  → preempts everything
  2. Normal rotation through enabled plugins in plugin_order

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
        """Set the priority interrupt plugin (flight tracker)."""
        self._flight_plugin = plugin

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
                self._dispatch()

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

    def _dispatch(self):
        # --- Priority interrupt: flight tracker ---
        if (
            self._flight_plugin is not None
            and self._flight_plugin.has_live_priority()
            and self._flight_plugin.has_live_content()
        ):
            if self._active_id != "flight_tracker":
                self._switch_to("flight_tracker")
            self._flight_plugin.draw()
            self.display.swap()
            return

        # --- Return from flight tracker when sky is clear ---
        if self._active_id == "flight_tracker":
            candidate_id = self._rotation[self._rotation_index]
            candidate = self._plugins.get(candidate_id)
            if candidate is not None and not candidate.has_content():
                self._advance_rotation()
            else:
                self._switch_to(candidate_id)

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
        try:
            active.draw()
        except Exception as e:
            log.error(f"[plugins] Draw error ({self._active_id}): {e}")

        self.display.swap()

    def _active_plugin(self):
        return self._plugins.get(self._active_id)

    def _switch_to(self, plugin_id: str):
        if plugin_id not in self._plugins:
            return
        old = self._active_id
        self._active_id = plugin_id
        self._active_start = time.monotonic()
        if old != plugin_id:
            self.display.clear()
            log.debug(f"[plugins] Switched: {old} → {plugin_id}")
        plugin = self._plugins[plugin_id]
        try:
            plugin.reset()
        except Exception as e:
            log.error(f"[plugins] Reset error ({plugin_id}): {e}")

    def _advance_rotation(self):
        if not self._rotation:
            return
        for _ in range(len(self._rotation)):
            self._rotation_index = (self._rotation_index + 1) % len(self._rotation)
            candidate_id = self._rotation[self._rotation_index]
            candidate = self._plugins.get(candidate_id)
            if candidate is None or candidate.has_content():
                self._switch_to(candidate_id)
                return
        # All plugins skipped — stay on current without switching

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
            for pid, plugin in list(self._plugins.items()):
                if not plugin.enabled:
                    continue
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

    def force_plugin(self, plugin_id: str) -> bool:
        """Switch to a specific plugin immediately. Returns False if unknown."""
        if plugin_id not in self._plugins:
            return False
        with self._lock:
            self._switch_to(plugin_id)
        return True

    def reload_rotation(self):
        """Re-read plugin_order from config and rebuild rotation."""
        self.build_rotation()
        if self._rotation:
            self._rotation_index = 0
            self._switch_to(self._rotation[0])

    def get_status(self) -> dict:
        return {
            "active_plugin": self._active_id,
            "rotation": self._rotation,
            "rotation_index": self._rotation_index,
            "active_since": self._active_start,
            "plugins": {
                pid: {"enabled": p.enabled, "has_live": p.has_live_content()}
                for pid, p in self._plugins.items()
            },
        }
