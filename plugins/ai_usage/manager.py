"""
AiUsagePlugin — Claude / Codex subscription usage (5-hour "Current" and
weekly limits) with colour-coded bars and reset times.

Config (plugins.ai_usage):
  providers             {"claude": {enabled, credentials_path}, "codex": {...}}
                        credentials_path "" → the CLI's default file (see providers.py)
  seconds_per_provider  each enabled provider gets its own page for this long
  reset_format          "times" | "countdown" | "both" (alternates every 3 s) | "off"
  update_interval       seconds between usage polls (min 60; default 300)

The last good result per provider is kept in data/ai_usage.json so the board
has numbers straight after a restart.
"""

import json
import logging
import os
import threading
import time
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.ai_usage import providers
from plugins.ai_usage.render import render, render_message
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

PROVIDER_ORDER = ("claude", "codex")
BOTH_PHASE_SECONDS = 3
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_FILE = os.path.join(_BASE_DIR, "data", "ai_usage.json")


def enabled_providers(config: dict) -> list:
    cfg = config.get("providers") or {"claude": {"enabled": True}}
    return [p for p in PROVIDER_ORDER if (cfg.get(p) or {}).get("enabled")]


def credentials_path(config: dict, provider: str) -> str:
    return ((config.get("providers") or {}).get(provider) or {}).get("credentials_path") or ""


def clock_format() -> str:
    try:
        from core.config_manager import get_config
        return get_config().get("location", "clock_format", default="12hr") or "12hr"
    except Exception:
        return "12hr"


class AiUsagePlugin(BasePlugin):
    PLUGIN_ID = "ai_usage"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        # provider → {"usage": dict | None, "error": str | None, "login": bool}
        self._results: dict = {}
        self._in_flight: set = set()
        self._failing: set = set()        # providers mid failure streak (warn once)
        self._start = time.monotonic()
        self._key: Optional[tuple] = None
        self._load_cache()

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _load_cache(self):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            for p, usage in cached.items():
                if p in PROVIDER_ORDER and isinstance(usage, dict):
                    self._results[p] = {"usage": usage, "error": None, "login": False}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.debug(f"[ai_usage] cache load error: {e}")

    def _save_cache(self):
        with self._lock:
            data = {p: r["usage"] for p, r in self._results.items() if r.get("usage")}
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            log.debug(f"[ai_usage] cache save error: {e}")

    # ------------------------------------------------------------------
    # Updates
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        # Off unless configured: existing installs have no ai_usage block, and the
        # update loop polls every enabled plugin even when it isn't in plugin_order.
        return bool(self.config.get("enabled", False))

    @property
    def update_interval(self) -> float:
        return max(60.0, float(self.config.get("update_interval", 300)))

    def update(self):
        """One background fetch per provider; never blocks the shared update thread."""
        for p in enabled_providers(self.config):
            with self._lock:
                if p in self._in_flight:
                    continue
                self._in_flight.add(p)
            threading.Thread(target=self._poll, args=(p,), daemon=True,
                             name=f"ai-usage-{p}").start()

    def _poll(self, p: str):
        try:
            usage = providers.fetch(p, credentials_path(self.config, p))
            result = {"usage": usage, "error": None, "login": False}
            if p in self._failing:
                log.info(f"[ai_usage] {p}: usage fetch recovered")
                self._failing.discard(p)
        except providers.LoginNeeded as e:
            result = {"usage": None, "error": str(e), "login": True}
            self._warn(p, f"login needed — {e}")
        except Exception as e:
            with self._lock:
                prev = (self._results.get(p) or {}).get("usage")
            result = {"usage": prev, "error": str(e), "login": False}   # keep showing last good values
            self._warn(p, f"usage fetch failed — {e}")
        with self._lock:
            self._in_flight.discard(p)
            self._results[p] = result
        if result["usage"] and not result["error"]:
            self._save_cache()

    def _warn(self, p: str, msg: str):
        if p not in self._failing:
            log.warning(f"[ai_usage] {p}: {msg}")
            self._failing.add(p)

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self.reset()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _pages(self) -> list:
        with self._lock:
            results = dict(self._results)
        return [(p, results[p]) for p in enabled_providers(self.config) if p in results]

    def _seconds_per_page(self) -> float:
        try:
            return max(3.0, float(self.config.get("seconds_per_provider", 10)))
        except (TypeError, ValueError):
            return 10.0

    def has_content(self) -> bool:
        return bool(self._pages())

    @property
    def display_duration(self) -> float:
        return self._seconds_per_page() * max(1, len(self._pages()))

    def reset(self):
        self._start = time.monotonic()
        self._key = None

    def is_cycle_complete(self) -> bool:
        return time.monotonic() - self._start >= self.display_duration

    def draw(self) -> bool:
        pages = self._pages()
        if not pages:
            return False
        elapsed = time.monotonic() - self._start
        p, result = pages[int(elapsed // self._seconds_per_page()) % len(pages)]
        usage = result.get("usage")
        fmt = self.config.get("reset_format", "times")
        phase = int(time.time() // BOTH_PHASE_SECONDS) % 2 if fmt == "both" else 0
        minute = int(time.time() // 60)        # countdowns and passed resets change by the minute
        key = (p, id(result), fmt, phase, minute, clock_format())
        if key == self._key:
            return True
        if usage:
            img = render(p, usage, fmt, clock_format(), phase)
        elif result.get("login"):
            img = render_message(p, "LOGIN NEEDED", "SEE CONFIG PAGE")
        else:
            img = render_message(p, "NO DATA", "RETRYING")
        push_pil_to_canvas(img, self.display_manager)
        self._key = key
        return True
