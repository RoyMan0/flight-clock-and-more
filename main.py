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
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_FILE   = "workdammit.log"
_LOG_MAX_BYTES  = 10 * 1024 * 1024   # 10 MB per file
_LOG_BACKUPS    = 2                   # keep current + 2 rotated copies (~30 MB max)

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            _LOG_FILE,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        ),
    ],
)
# Suppress noisy third-party loggers
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # Flask access log
logging.getLogger("httpx").setLevel(logging.WARNING)     # FR24 HTTP request logs
logging.getLogger("fr24").setLevel(logging.WARNING)      # FR24 anonymous access message
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
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Force first-boot setup mode regardless of WiFi state",
    )
    parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip the WiFi setup check on startup",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logging.getLogger().setLevel(args.log_level)

    # Import all modules BEFORE hardware init — RGBMatrix drops privileges after
    # init, and root-owned __pycache__ files block imports that happen afterwards.
    # Live-preview mirror: must wrap rgbmatrix.graphics before any Font is created
    # (setup/fonts.py loads fonts at import time).
    from core import canvas_mirror
    canvas_mirror.install()
    from core.config_manager import get_config
    from core.display_manager import DisplayManager
    from core.plugin_manager import PluginManager
    from core.bootstrap import import_plugins, register_plugins
    plugin_classes = import_plugins()

    create_app = None
    try:
        from web.app import create_app
    except Exception as e:
        log.warning(f"Web server failed to import: {e}")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    cfg = get_config()

    # ------------------------------------------------------------------
    # Setup mode — must run BEFORE DisplayManager (which drops root)
    # run_setup_mode() never returns; it os.execv's back into this script
    # with --skip-setup once WiFi is connected, or is killed by systemctl.
    # ------------------------------------------------------------------
    if not args.skip_setup:
        should_enter = False
        forced = False
        if args.no_hardware:
            should_enter = forced = args.setup
        else:
            from core.setup_mode import is_wifi_client_connected, setup_requested
            explicitly = args.setup or setup_requested()
            should_enter = explicitly or not is_wifi_client_connected()
            forced = explicitly
        if should_enter:
            from core.setup_mode import run_setup_mode
            log.info("Entering first-boot setup mode%s", " (forced)" if forced else "")
            run_setup_mode(cfg, args, forced=forced)   # does not return

    # ------------------------------------------------------------------
    # Display (hardware init — drops root privileges here)
    # ------------------------------------------------------------------
    display_cfg = cfg.get("display") or {}
    display = DisplayManager(display_cfg, software_mode=args.no_hardware)
    log.info(f"Display initialised ({'software' if args.no_hardware else 'hardware'} mode)")

    # ------------------------------------------------------------------
    # Plugins
    # ------------------------------------------------------------------
    pm = PluginManager(display, cfg)

    register_plugins(pm, display, cfg, plugin_classes)

    # ------------------------------------------------------------------
    # Web server (background thread)
    # ------------------------------------------------------------------
    if create_app:
        try:
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
