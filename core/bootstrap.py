"""
Plugin registration shared by the Pi entry point (main.py) and the Mac desktop app.

Every optional plugin is imported inside try/except so one broken module (or a
missing dependency) never stops the rest of the display from starting.
Import modules BEFORE the hardware is initialised — RGBMatrix drops privileges,
and root-owned __pycache__ files would then block later imports.
"""

import importlib
import logging

log = logging.getLogger(__name__)

# (plugin id, module, class, readable name for log lines) — in registration order
OPTIONAL_PLUGINS = [
    ("stock_ticker",            "plugins.stock_ticker.manager",            "StockTickerPlugin",           "Stock ticker"),
    ("snow_report",             "plugins.snow_report.manager",             "SnowReportPlugin",            "Snow report"),
    ("sports",                  "plugins.sports.manager",                  "SportsPlugin",                "Sports"),
    ("specific_flight_tracker", "plugins.specific_flight_tracker.manager", "SpecificFlightTrackerPlugin", "Specific flight tracker"),
    ("world_daylight",          "plugins.world_daylight.manager",          "WorldDaylightPlugin",         "World daylight"),
    ("ap_news",                 "plugins.ap_news.manager",                 "APNewsPlugin",                "AP news"),
    ("event_countdown",         "plugins.event_countdown.manager",         "EventCountdownPlugin",        "Event countdown"),
    ("transit",                 "plugins.transit.manager",                 "TransitPlugin",               "Transit"),
    ("world_clock",             "plugins.world_clock.manager",             "WorldClockPlugin",            "World clock"),
    ("now_playing",             "plugins.now_playing.manager",             "NowPlayingPlugin",            "Now playing"),
    ("system_stats",            "plugins.system_stats.manager",            "SystemStatsPlugin",           "System stats"),
    ("ai_usage",                "plugins.ai_usage.manager",                "AiUsagePlugin",               "AI usage"),
    ("calendar",                "plugins.calendar.manager",                "CalendarPlugin",              "Calendar"),
    ("maze",                    "plugins.maze.manager",                    "MazePlugin",                  "Maze"),
]


def import_plugins():
    """Import every plugin class. Returns {plugin id: class}; failures are logged and skipped."""
    from plugins.clock_weather.manager import ClockWeatherPlugin
    from plugins.flight_tracker.manager import FlightTrackerPlugin

    classes = {"clock_weather": ClockWeatherPlugin, "flight_tracker": FlightTrackerPlugin}
    for pid, module, cls, label in OPTIONAL_PLUGINS:
        try:
            classes[pid] = getattr(importlib.import_module(module), cls)
        except Exception as e:
            log.warning(f"{label} plugin failed to import: {e}")
    return classes


def register_plugins(pm, display, cfg, classes):
    """Instantiate and register the imported plugins on the plugin manager."""
    secrets = cfg.get_all_secrets()

    pm.register("clock_weather", classes["clock_weather"](
        display, cfg.get_plugin_config("clock_weather"), secrets
    ))

    ft = classes["flight_tracker"](display, cfg.get_plugin_config("flight_tracker"), secrets)
    pm.register("flight_tracker", ft)
    pm.set_flight_plugin(ft)

    for pid, _module, _cls, label in OPTIONAL_PLUGINS:
        plugin_cls = classes.get(pid)
        if not plugin_cls:
            continue
        try:
            pm.register(pid, plugin_cls(display, cfg.get_plugin_config(pid), secrets))
        except Exception as e:
            log.warning(f"{label} plugin failed to load: {e}")
