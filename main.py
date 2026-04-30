#!/usr/bin/env python3
"""
main.py — entry point for the LED matrix plugin system.

Usage:
  sudo python3 main.py                 # normal hardware mode
  python3 main.py --no-hardware        # software/dev mode (no Pi required)
"""

import sys
import argparse
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("workdammit.log", mode="a"),
    ],
)
log = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="LED Matrix Plugin Display")
    parser.add_argument(
        "--no-hardware",
        action="store_true",
        help="Run in software mode (no LED matrix required)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logging.getLogger().setLevel(args.log_level)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    from core.config_manager import get_config
    cfg = get_config()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    from core.display_manager import DisplayManager
    display_cfg = cfg.get("display") or {}
    display = DisplayManager(display_cfg, software_mode=args.no_hardware)
    log.info(f"Display initialised ({'software' if args.no_hardware else 'hardware'} mode)")

    # ------------------------------------------------------------------
    # Plugins
    # ------------------------------------------------------------------
    from core.plugin_manager import PluginManager
    pm = PluginManager(display, cfg)

    secrets = {k: cfg.get_secret(k) for k in [
        "tomorrow_api_key", "schwab_app_key", "schwab_app_secret",
        "schwab_token_path", "schwab_account_hash",
        "email_sender", "email_password",
    ]}

    # Clock/Weather
    from plugins.clock_weather.manager import ClockWeatherPlugin
    pm.register("clock_weather", ClockWeatherPlugin(
        display, cfg.get_plugin_config("clock_weather"), secrets
    ))

    # Flight Tracker (priority interrupt — registered but NOT in rotation)
    from plugins.flight_tracker.manager import FlightTrackerPlugin
    ft = FlightTrackerPlugin(display, cfg.get_plugin_config("flight_tracker"), secrets)
    pm.register("flight_tracker", ft)
    pm.set_flight_plugin(ft)

    # Stock Ticker
    try:
        from plugins.stock_ticker.manager import StockTickerPlugin
        pm.register("stock_ticker", StockTickerPlugin(
            display, cfg.get_plugin_config("stock_ticker"), secrets
        ))
    except Exception as e:
        log.warning(f"Stock ticker plugin failed to load: {e}")

    # Snow Report
    try:
        from plugins.snow_report.manager import SnowReportPlugin
        pm.register("snow_report", SnowReportPlugin(
            display, cfg.get_plugin_config("snow_report"), secrets
        ))
    except Exception as e:
        log.warning(f"Snow report plugin failed to load: {e}")

    # Sports
    try:
        from plugins.sports.manager import SportsPlugin
        pm.register("sports", SportsPlugin(
            display, cfg.get_plugin_config("sports"), secrets
        ))
    except Exception as e:
        log.warning(f"Sports plugin failed to load: {e}")

    # ------------------------------------------------------------------
    # Web server (background thread)
    # ------------------------------------------------------------------
    try:
        from web.app import create_app
        app = create_app(cfg, display, pm)
        web_cfg = cfg.get("web") or {}
        web_thread = threading.Thread(
            target=lambda: app.run(
                host=web_cfg.get("host", "0.0.0.0"),
                port=web_cfg.get("port", 8080),
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
            name="web-server",
        )
        web_thread.start()
        log.info(f"Web server started on port {web_cfg.get('port', 8080)}")
    except Exception as e:
        log.warning(f"Web server failed to start: {e}")

    # ------------------------------------------------------------------
    # Run main loop (blocks)
    # ------------------------------------------------------------------
    pm.run()


if __name__ == "__main__":
    main()
