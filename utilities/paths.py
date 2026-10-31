"""
Where the code lives vs. where it may write.

APP_ROOT   the repo / bundle: code, fonts, logos, icons, airports.json, example configs.
DATA_ROOT  everything the app writes: config/, data/, .cache/, close.txt, maps.

On the Pi both are the repo directory, so nothing moves. The Mac app sets
FLIGHTCLOCK_DATA_DIR (~/Library/Application Support/Flight Clock) because its
bundle is read-only. New code that writes files must build the path from DATA_ROOT.
"""

import os

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.abspath(os.environ.get("FLIGHTCLOCK_DATA_DIR") or APP_ROOT)

# True when running inside the Mac desktop app (set by its launcher)
DESKTOP = os.environ.get("FLIGHTCLOCK_DESKTOP") == "1"
