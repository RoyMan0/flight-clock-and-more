"""
Generic "Test" preview for modules without their own preview endpoint.

A throwaway instance of the module is built with the config page's UNSAVED
settings and drawn onto an off-screen software display; the frames come back
as images. It never touches the real panel or the live module's state.

Data, per module (so pressing Test never burns paid/capped API quota):
  free sources fetch fresh   — sports (ESPN), stock ticker (Yahoo/Schwab), breaking news (AP)
  cached data only           — clock & weather, world daylight (cached weather readers)
  live module's data copied  — specific flight tracker (no AirLabs lookups; no poll thread)
  no data needed             — event countdown
Overhead Flights isn't offered: it only has something to show when a plane is overhead.
"""

import copy
import logging

log = logging.getLogger(__name__)

PREVIEWABLE = ("clock_weather", "specific_flight_tracker", "stock_ticker", "sports",
               "world_daylight", "ap_news", "event_countdown")

# How many draw() calls to run, and which ones to capture (frames advance per call)
_DRAWS = {
    "clock_weather":           (60, [59]),             # scenes fill in over the first frames
    "specific_flight_tracker": (40, [39]),
    "stock_ticker":            (90, [0, 45, 89]),      # scrolling
    "world_daylight":          (5, [4]),
    "ap_news":                 (120, [0, 60, 119]),    # scrolling headlines
    "event_countdown":         (3, [2]),
}
MAX_FRAMES = 4


class PreviewError(Exception):
    pass


def _fake_display():
    from core.display_manager import DisplayManager
    return DisplayManager({}, software_mode=True)


def _snapshot(display):
    return display.get_snapshot().copy()


def render(pm, pid: str, unsaved: dict, secrets: dict) -> tuple:
    """(frames: [PIL.Image], message: str). Raises PreviewError for setup problems."""
    if pid not in PREVIEWABLE:
        raise PreviewError("This module doesn't have a preview")
    live = pm._plugins.get(pid) if pm else None
    if live is None:
        raise PreviewError("This module isn't loaded")
    config = {**live.config, **(unsaved or {}), "enabled": True}
    display = _fake_display()

    if pid == "specific_flight_tracker":
        # Disabled so the constructor doesn't start an AirLabs poll thread; then
        # copy the live module's already-fetched flights for the callsigns in the form.
        tmp = type(live)(display, {**config, "enabled": False}, secrets)
        tmp.config["enabled"] = True
        wanted = [c.strip().upper() for c in config.get("tracked_callsigns", []) if str(c).strip()]
        with live._lock:
            tmp._flights = {cs: copy.deepcopy(live._flights[cs]) for cs in wanted if cs in live._flights}
        if not tmp.has_content():
            new = [cs for cs in wanted if cs not in live._flights]
            if new:
                return [], (f"Save first to start tracking {', '.join(new)} — flights appear here once "
                            "they're airborne.")
            return [], "None of these flights are in the air right now."
    else:
        tmp = type(live)(display, config, secrets)

    if pid == "sports":
        tmp.update()
        games = list(tmp._games)
        if not games:
            return [], ("No games to show right now with these settings — check the leagues, your favourite "
                        "teams, and the upcoming / today-only options.")
        from plugins.sports.manager import _render_game
        return [_render_game(g) for g in games[:MAX_FRAMES]], (
            f"Showing {min(len(games), MAX_FRAMES)} of {len(games)} games." if len(games) > MAX_FRAMES else "")

    if pid in ("stock_ticker", "ap_news", "clock_weather", "world_daylight"):
        tmp.update()

    try:
        tmp.reset()
    except Exception as e:
        log.debug(f"[preview] {pid} reset: {e}")
    draws, capture = _DRAWS.get(pid, (5, [4]))
    frames, drew_any = [], False
    for i in range(draws):
        try:
            drew = tmp.draw()
        except Exception as e:
            raise PreviewError(f"Couldn't draw this module: {e}")
        drew_any = drew_any or bool(drew)
        if i in capture:
            frames.append(_snapshot(display))
    if not drew_any:
        return [], "Nothing to show with these settings right now."
    # Drop identical frames (e.g. a ticker too short to scroll)
    unique = []
    for f in frames:
        if not unique or f.tobytes() != unique[-1].tobytes():
            unique.append(f)
    return unique[:MAX_FRAMES], ""
