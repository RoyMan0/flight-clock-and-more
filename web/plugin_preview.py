"""
Generic "Test" preview for modules without their own preview endpoint.

A throwaway instance of the module is built with the config page's UNSAVED
settings and drawn onto an off-screen software display; the frames come back
as images. It never touches the real panel or the live module's state.

Data, per module (so pressing Test never burns paid/capped API quota):
  free sources fetch fresh   — sports (ESPN), stock ticker (Yahoo/Schwab), breaking news (AP)
  cached data only           — clock & weather, world daylight (cached weather readers)
  live module's data copied  — specific flight tracker (no AirLabs lookups; no poll thread)
  no data needed             — event countdown, maze (built and solved off-screen)
  polled fresh               — system stats (every enabled machine, twice ~2 s apart so rates show)
Overhead Flights isn't offered: it only has something to show when a plane is overhead.
"""

import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

PREVIEWABLE = ("clock_weather", "specific_flight_tracker", "stock_ticker", "sports",
               "world_daylight", "ap_news", "event_countdown", "maze", "system_stats")

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


def _maze(config: dict) -> tuple:
    """A maze with these settings, solved off-screen: start, halfway, and the finish."""
    from plugins.maze.manager import MazePlugin
    from plugins.maze.render import THEMES, render_celebration, render_solving, render_stats
    tmp = MazePlugin(_fake_display(), config, {})
    tmp._new_maze()
    maze, layout, theme = tmp._maze, tmp._layout, tmp._theme
    halfway = copy.deepcopy(tmp._solver)                 # solvers are seeded, so the copy replays the same run
    solver, steps = tmp._solver, 0
    while not solver.done and steps < 100_000:
        solver.step()
        steps += 1
    for _ in range(steps // 2):
        halfway.step()
    frames = [render_solving(layout, maze, theme, halfway, 0)]
    if config.get("celebrate", True):
        cel = render_celebration(layout, maze, theme, solver.path, 15)
        frames += [cel, render_stats(cel, ["SOLVED", f"{solver.dead_ends} DEAD ENDS", f"{solver.steps} STEPS"],
                                     THEMES[theme]["routes"][0])]
    else:
        frames.append(render_solving(layout, maze, theme, solver, 0))
    name = {"Explorer": "Explorer", "WallFollower": "Wall follower", "Flood": "Flood"}.get(
        type(solver).__name__, type(solver).__name__)
    return frames, f"Halfway, then solved — {name} solver, {theme} theme. Each Test builds a new maze."


def _system_stats(config: dict) -> tuple:
    """Every enabled machine polled twice (in parallel) so disk/network rates show."""
    from plugins.system_stats.collect import Collector
    from plugins.system_stats.manager import enabled_connections, units
    from plugins.system_stats.render import (
        MATRIX_H, build_sections, build_strip, needs_scroll, render_static, render_strip_frame,
    )
    conns = enabled_connections(config)
    if not conns:
        return [], "No machines are turned on."

    def poll(conn):
        collector = Collector(conn)                      # fresh: never disturbs the live poller
        try:
            collector.sample()
            time.sleep(2)
            return conn, collector.sample(), None
        except Exception as e:
            return conn, None, str(e)

    with ThreadPoolExecutor(max_workers=min(8, len(conns))) as ex:
        results = list(ex.map(poll, conns))
    sections = build_sections(results, units())
    offline = [r[0].get("name") or r[0].get("host") or "This Pi" for r in results if r[1] is None]
    note = f" Couldn't reach {', '.join(offline)} — use Test this machine to see why." if offline else ""
    if not needs_scroll(sections):
        return [render_static(sections)], note.strip()
    strip = build_strip(sections)
    pages = max(1, -(-strip.height // MATRIX_H))
    frames = [render_strip_frame(strip, i * MATRIX_H) for i in range(min(pages, MAX_FRAMES))]
    shown = f"the first {MAX_FRAMES} of {pages} screens" if pages > MAX_FRAMES else "each screen"
    return frames, f"This scrolls on the panel — showing {shown}.{note}"


def render(pm, pid: str, unsaved: dict, secrets: dict) -> tuple:
    """(frames: [PIL.Image], message: str). Raises PreviewError for setup problems."""
    if pid not in PREVIEWABLE:
        raise PreviewError("This module doesn't have a preview")
    live = pm._plugins.get(pid) if pm else None
    if live is None:
        raise PreviewError("This module isn't loaded")
    config = {**live.config, **(unsaved or {}), "enabled": True}
    if pid == "maze":
        return _maze(config)
    if pid == "system_stats":
        return _system_stats(config)
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


# ------------------------------------------------------------------
# The desktop app's style browser: one LED frame per module, from live data
# ------------------------------------------------------------------

_SAMPLE_CACHE: dict = {}            # pid → (monotonic time, frame or None)
SAMPLE_TTL = 60.0


def _copy_state(live, display):
    """A throwaway copy of a running module with its current data, drawing on `display`.
    Values that can't be deep-copied (locks, threads, sessions) are shared, read-only."""
    tmp = object.__new__(type(live))
    lock = getattr(live, "_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        for k, v in live.__dict__.items():
            if k == "display_manager":
                continue
            try:
                tmp.__dict__[k] = copy.deepcopy(v)
            except Exception:
                tmp.__dict__[k] = v
    finally:
        if lock is not None:
            lock.release()
    tmp.display_manager = display
    return tmp


def sample_frame(pm, pid: str, secrets: dict):
    """The module's LED picture right now (a PIL image), or None when it has nothing to show
    (not set up, no data yet). Never touches the real panel or the live module."""
    hit = _SAMPLE_CACHE.get(pid)
    if hit and time.monotonic() - hit[0] < SAMPLE_TTL:
        return hit[1]
    frame = None
    try:
        if pid in PREVIEWABLE:
            frames, _msg = render(pm, pid, {}, secrets)
            frame = frames[-1] if frames else None
        else:
            live = pm._plugins.get(pid) if pm else None
            if live is not None and live.has_content():
                display = _fake_display()
                tmp = _copy_state(live, display)
                try:
                    tmp.reset()
                except Exception:
                    pass
                drew = False
                for _ in range(30):                     # let scenes and transitions settle
                    drew = bool(tmp.draw()) or drew
                frame = _snapshot(display) if drew else None
    except Exception as e:
        log.info(f"[preview] sample {pid}: {e}")
        frame = None
    _SAMPLE_CACHE[pid] = (time.monotonic(), frame)
    return frame
