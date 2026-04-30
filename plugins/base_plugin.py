"""
BasePlugin — abstract interface that every plugin must implement.

Lifecycle:
  1. __init__()  — called once at startup; do NOT make network calls here
  2. update()    — called in a background thread at update_interval; fetch data
  3. draw()      — called every frame (~10fps) when this plugin is active
  4. reset()     — called each time this plugin becomes the active display

Priority interrupt (flight tracker overrides all):
  plugin_manager checks has_live_priority() AND has_live_content() each frame.
  When both are True the plugin preempts whatever is currently showing.

Cycling:
  is_cycle_complete() returning True signals the manager to advance to the
  next plugin even if display_duration hasn't expired yet.
"""

from abc import ABC, abstractmethod


class BasePlugin(ABC):
    PLUGIN_ID = "base"

    def __init__(self, display_manager, config: dict, secrets: dict):
        self.display_manager = display_manager
        self.config = dict(config)
        self.secrets = dict(secrets)

    # ------------------------------------------------------------------
    # Required
    # ------------------------------------------------------------------

    @abstractmethod
    def draw(self) -> bool:
        """
        Render one frame to display_manager.canvas.
        Return True if content was drawn, False if nothing to show.
        """

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    def update(self):
        """Fetch / refresh external data. Called in background thread."""

    def reset(self):
        """Called when this plugin becomes the active display."""

    def on_config_change(self, new_config: dict):
        """Called by ConfigManager when web UI saves new settings."""
        self.config = dict(new_config)

    # ------------------------------------------------------------------
    # Priority
    # ------------------------------------------------------------------

    def has_live_priority(self) -> bool:
        """
        True if this plugin CAN interrupt the normal rotation.
        Evaluated every frame; only preempts when has_live_content() is also True.
        """
        return False

    def has_live_content(self) -> bool:
        """True if there is actually live content to display right now."""
        return False

    # ------------------------------------------------------------------
    # Cycling control
    # ------------------------------------------------------------------

    def is_cycle_complete(self) -> bool:
        """
        True when this plugin has shown all its content for this rotation pass.
        The manager will advance to the next plugin when this returns True,
        regardless of display_duration.
        """
        return False

    @property
    def display_duration(self) -> float:
        """Seconds to show this plugin before advancing (if cycle not complete)."""
        return float(self.config.get("display_duration", 30))

    @property
    def update_interval(self) -> float:
        """How often (seconds) to call update() in the background thread."""
        return float(self.config.get("update_interval", 60))

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))
