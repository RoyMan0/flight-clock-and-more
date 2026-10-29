"""
Fire-and-forget background work, at most one run per name at a time.

The display loop (core/plugin_manager.py) must never wait on the network: with
the internet down every request can hang for its full timeout plus a DNS
lookup that requests' timeout doesn't cover. Code reachable from draw()/reset()
or a scene keyframe calls run_once("name", fn) to refresh its cache and keeps
showing whatever it already has.
"""

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_running: set = set()


def run_once(name: str, fn, *args) -> bool:
    """Start fn(*args) on a daemon thread unless `name` is still running.
    Returns True if a new run was started."""
    with _lock:
        if name in _running:
            return False
        _running.add(name)

    def _run():
        try:
            fn(*args)
        except Exception as e:
            log.error(f"[background] {name} failed: {e}")
        finally:
            with _lock:
                _running.discard(name)

    threading.Thread(target=_run, daemon=True, name=f"bg-{name}").start()
    return True


def is_running(name: str) -> bool:
    with _lock:
        return name in _running
