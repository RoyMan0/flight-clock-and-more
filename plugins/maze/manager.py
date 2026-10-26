"""
MazePlugin — the Pi generates a maze and solves it on the panel (display
mode), or you play it from the Display tab (play mode).

Display mode cycle: solving → celebration (rainbow wave along the path) →
stats card → new maze. The maze and the solver's progress persist between
rotation turns, so a maze is only replaced once it's solved.

Play mode: the web API starts a Game (game.py) and pins this plugin; while a
game is active the panel shows it instead. It ends on "Return to Rotation",
"end", or after `play_idle_minutes` without input (the pin is released).

Config (plugins.maze): size S/M/L, style winding/branchy/loops/random,
solver explorer/wall/flood/rotate, solve_speed (steps per frame), theme
neon/pacman/retro/random, celebrate, play_idle_minutes.
"""

import logging
import random
import threading
import time
from typing import Optional

from plugins.base_plugin import BasePlugin
from plugins.maze.game import Game
from plugins.maze.generate import Layout, generate
from plugins.maze.render import (
    THEMES, pick_theme, render_celebration, render_play, render_solving, render_stats,
)
from plugins.maze.solve import SOLVERS, make_solver
from plugins.transit.render import push_pil_to_canvas

log = logging.getLogger(__name__)

CELEBRATE_FRAMES = 30          # ~3 s rainbow wave
STATS_FRAMES = 35              # ~3.5 s stats card


class MazePlugin(BasePlugin):
    PLUGIN_ID = "maze"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.RLock()
        self._rng = random.Random()
        self._rotate_idx = self._rng.randrange(len(SOLVERS))
        self._maze = None
        self._solver = None
        self._phase = "solving"
        self._phase_frame = 0
        self._frame = 0
        self._acc = 0.0
        self._theme = "neon"
        self.game: Optional[Game] = None
        self._game_theme = "pacman"
        self._last_input = 0.0
        self._on_game_end = None
        self._still_pinned = None

    # Off unless configured — installs from before this module have no config block
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def on_config_change(self, new_config: dict):
        old = dict(self.config)
        super().on_config_change(new_config)
        if any(old.get(k) != new_config.get(k) for k in ("size", "style", "solver", "theme")):
            with self._lock:
                self._maze = None                      # start a fresh maze with the new settings

    def update(self):
        pass                                           # all local — nothing to fetch

    def has_content(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Display mode
    # ------------------------------------------------------------------

    def _new_maze(self):
        size = self.config.get("size", "M")
        self._layout = Layout(size)
        self._maze = generate(size, self.config.get("style", "random"))
        solver = self.config.get("solver", "rotate")
        if solver not in SOLVERS:
            # Rotate; a wall follower in a maze with loops would just hand over, so skip it there
            choices = [s for s in SOLVERS if not (s == "wall" and self._maze.style == "loops")]
            solver = choices[self._rotate_idx % len(choices)]
            self._rotate_idx += 1
        self._solver = make_solver(solver, self._maze, seed=self._maze.seed)
        self._theme = pick_theme(self.config.get("theme", "random"), self._rng)
        self._phase, self._phase_frame, self._acc = "solving", 0, 0.0
        log.debug(f"[maze] new {size} {self._maze.style} maze, solver={solver}, theme={self._theme}")

    def _speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("solve_speed", 2)))
        except (TypeError, ValueError):
            return 2.0

    def _display_frame(self):
        if self._maze is None:
            self._new_maze()
        if self._phase == "solving":
            self._acc += self._speed()
            while self._acc >= 1 and not self._solver.done:
                self._acc -= 1
                self._solver.step()
            img = render_solving(self._layout, self._maze, self._theme, self._solver, self._frame)
            if self._solver.done:
                self._phase, self._phase_frame = ("celebrate" if self.config.get("celebrate", True) else "done"), 0
            return img
        if self._phase == "celebrate":
            self._phase_frame += 1
            img = render_celebration(self._layout, self._maze, self._theme, self._solver.path, self._phase_frame)
            if self._phase_frame >= CELEBRATE_FRAMES:
                self._phase, self._phase_frame = "stats", 0
                self._stats_base = img
            return img
        if self._phase == "stats":
            self._phase_frame += 1
            s = self._solver
            img = render_stats(self._stats_base, ["SOLVED", f"{s.dead_ends} DEAD ENDS", f"{s.steps} STEPS"],
                               THEMES[self._theme]["routes"][0])
            if self._phase_frame >= STATS_FRAMES:
                self._new_maze()
            return img
        self._new_maze()                                # "done" without celebration
        return render_solving(self._layout, self._maze, self._theme, self._solver, self._frame)

    # ------------------------------------------------------------------
    # Play mode
    # ------------------------------------------------------------------

    def start_game(self, size=None, mode="free", difficulty="medium", on_end=None, still_pinned=None) -> Game:
        with self._lock:
            self.game = Game(size or self.config.get("size", "M"), mode, difficulty)
            theme = self.config.get("theme", "random")
            self._game_theme = "pacman" if mode == "pacman" else pick_theme(theme, self._rng)
            self._last_input = time.monotonic()
            self._on_game_end = on_end
            self._still_pinned = still_pinned       # () -> bool: is the maze still pinned on the panel?
            return self.game

    def touch(self):
        self._last_input = time.monotonic()

    def end_game(self, release=True):
        with self._lock:
            cb, self._on_game_end = self._on_game_end, None
            self.game = None
        if release and cb:
            try:
                cb()
            except Exception as e:
                log.debug(f"[maze] release failed: {e}")

    def game_theme(self) -> dict:
        return THEMES[self._game_theme]

    def _idle_minutes(self) -> float:
        try:
            return max(0.5, float(self.config.get("play_idle_minutes", 3)))
        except (TypeError, ValueError):
            return 3.0

    def _play_frame(self, game: Game):
        game.tick()
        st = game.render_state()
        if st["giveup"] is not None:
            return render_solving(game.layout, game.maze, self._game_theme, st["giveup"], self._frame)
        return render_play(game.layout, game.maze, self._game_theme, st, self._frame)

    # ------------------------------------------------------------------

    def draw(self) -> bool:
        self._frame += 1
        with self._lock:
            game = self.game
            idle_end = False
            if game and self._still_pinned and not self._still_pinned():
                # "Return to Rotation" (Display page or Dashboard) unpinned us — end the game quietly
                self.game = None
                game = None
            elif game and time.monotonic() - self._last_input > self._idle_minutes() * 60:
                log.info("[maze] play session idle — returning to rotation")
                game = None
                idle_end = True
            img = self._play_frame(game) if game else self._display_frame()
        push_pil_to_canvas(img, self.display_manager)
        if idle_end:
            self.end_game()
        return True
