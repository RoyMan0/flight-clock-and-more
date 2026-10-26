"""
Play mode — one shared game session driven from the Display page.

Modes:
  free    reach the exit; timer + moves; best times per size
  race    the Pi (Explorer solver) races you from the same start — first to the exit wins
  pacman  braided maze with dots on every cell; eat them all; ghosts chase you; 3 lives
  fog     only cells near you are visible (explored cells stay faint)

All modes: hint (flashes the next cell toward the goal), give up (the Pi
finishes from where you are with the flood animation).
State changes bump `version` so the browser can poll cheaply.
"""

import json
import os
import random
import threading
import time
from datetime import date

from plugins.maze.generate import DIR_NAMES, Layout, generate
from plugins.maze.solve import Explorer, Flood

MODES = ("free", "race", "pacman", "fog")
DIFFICULTY = {  # race: Pi steps/s · pacman: ghosts, ghost cells/s
    "easy":   {"race": 2.5, "ghosts": 2, "ghost_speed": 1.6},
    "medium": {"race": 4.0, "ghosts": 3, "ghost_speed": 2.3},
    "hard":   {"race": 6.5, "ghosts": 4, "ghost_speed": 3.0},
}
FOG_RADIUS = {"S": 2, "M": 3, "L": 3}
LIVES = 3
CHASE_RANGE = 6
HURT_SECS = 1.5
HINT_SECS = 2.0

_BEST_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "maze", "best.json"))
_best_lock = threading.Lock()


def load_best() -> dict:
    try:
        with open(_BEST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _record_best(key: str, entry: dict) -> int:
    """Insert into the top-5 list for key; return its rank (1-based) or 0."""
    with _best_lock:
        best = load_best()
        lst = best.get(key, [])
        lst.append(entry)
        lst.sort(key=lambda e: (e["secs"], e.get("moves", 0)))
        lst = lst[:5]
        best[key] = lst
        os.makedirs(os.path.dirname(_BEST_PATH), exist_ok=True)
        tmp = _BEST_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(best, f)
        os.replace(tmp, _BEST_PATH)
        return lst.index(entry) + 1 if entry in lst else 0


class Game:
    def __init__(self, size="M", mode="free", difficulty="medium", seed=None):
        self.size = size if size in ("S", "M", "L") else "M"
        self.mode = mode if mode in MODES else "free"
        self.difficulty = difficulty if difficulty in DIFFICULTY else "medium"
        self.layout = Layout(self.size)
        self.maze = generate(self.size, "loops" if self.mode == "pacman" else "random", seed)
        self.rng = random.Random(self.maze.seed)
        self.lock = threading.RLock()
        self.version = 0
        self.player = self.maze.start
        self.trail = {self.player}
        self.moves = 0
        self.started = None
        self.finished = None
        self.status = "ready"            # ready → playing → won / lost / gaveup
        self.result = ""
        self.rank = 0
        self.hints = 0
        self.hint_cell = None
        self.hint_until = 0.0
        self.giveup_solver = None
        cfg = DIFFICULTY[self.difficulty]
        self.racer = Explorer(self.maze, seed=self.maze.seed) if self.mode == "race" else None
        self._racer_acc = 0.0
        self.dots = set()
        self.ghosts = []
        self.lives = LIVES
        self.score = 0
        self.hurt_until = 0.0
        self._ghost_acc = 0.0
        self._ghost_prev = []
        if self.mode == "pacman":
            self.dots = {(x, y) for y in range(self.maze.rows) for x in range(self.maze.cols)} - {self.player}
            dist = self.maze.distances(self.maze.start)
            far = [c for c, d in dist.items() if d >= max(6, max(dist.values()) // 2)]
            self.rng.shuffle(far)
            self._ghost_spawn = far[:cfg["ghosts"]]
            self.ghosts = list(self._ghost_spawn)
            self._ghost_prev = [None] * len(self.ghosts)
        self.seen = set()
        self.visible = None
        self._update_fog()
        self._last_tick = time.monotonic()

    # ------------------------------------------------------------------
    def _bump(self):
        self.version += 1

    def _update_fog(self):
        if self.mode != "fog":
            return
        r = FOG_RADIUS[self.size]
        px, py = self.player
        self.visible = {(x, y) for y in range(py - r, py + r + 1) for x in range(px - r, px + r + 1)
                        if 0 <= x < self.maze.cols and 0 <= y < self.maze.rows}
        self.seen |= self.visible

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started

    def _finish(self, status: str, result: str):
        self.status = status
        self.finished = time.time()
        self.result = result
        if status == "won" and self.hints == 0:
            secs = round(self.elapsed(), 1)
            self.rank = _record_best(f"{self.mode}:{self.size}", {
                "secs": secs, "moves": self.moves, "date": date.today().isoformat(),
                "difficulty": self.difficulty if self.mode in ("race", "pacman") else "",
                "score": self.score if self.mode == "pacman" else None,
            })

    # ------------------------------------------------------------------
    def move(self, direction: str) -> bool:
        d = DIR_NAMES.get(direction)
        with self.lock:
            if d is None or self.status in ("won", "lost", "gaveup"):
                return False
            nxt = self.maze.move(self.player, d)
            if nxt is None:
                return False                           # wall
            if self.status == "ready":
                self.status = "playing"
                self.started = time.time()
            self.player = nxt
            self.moves += 1
            self.trail.add(nxt)
            self.hint_cell = None
            self._update_fog()
            if self.mode == "pacman":
                if nxt in self.dots:
                    self.dots.discard(nxt)
                    self.score += 10
                self._check_ghosts()
                if not self.dots and self.status == "playing":
                    self._finish("won", f"Cleared! {self.score} pts")
            elif nxt == self.maze.exit:
                if self.mode == "race" and self.racer and self.racer.done:
                    self._finish("lost", "The Pi got there first")
                else:
                    self._finish("won", "You beat the Pi!" if self.mode == "race" else "Solved!")
            self._bump()
            return True

    def hint(self):
        with self.lock:
            if self.status in ("won", "lost", "gaveup"):
                return None
            if self.mode == "pacman" and self.dots:
                dist = self.maze.distances(self.player)
                goal = min(self.dots, key=lambda c: dist.get(c, 1e9))
            else:
                goal = self.maze.exit
            path = self.maze.shortest_path(self.player, goal)
            if len(path) < 2:
                return None
            self.hints += 1
            self.hint_cell = path[1]
            self.hint_until = time.monotonic() + HINT_SECS
            self._bump()
            dx, dy = path[1][0] - self.player[0], path[1][1] - self.player[1]
            return {(0, -1): "up", (1, 0): "right", (0, 1): "down", (-1, 0): "left"}[(dx, dy)]

    def give_up(self):
        with self.lock:
            if self.status in ("won", "lost", "gaveup"):
                return
            self.giveup_solver = Flood(self.maze, start=self.player)
            self._finish("gaveup", "The Pi is finishing it…")
            self._bump()

    # ------------------------------------------------------------------
    def tick(self):
        """Advance the racer / ghosts / give-up animation. Called every frame."""
        now = time.monotonic()
        dt, self._last_tick = now - self._last_tick, now
        with self.lock:
            changed = False
            if self.hint_cell and now > self.hint_until:
                self.hint_cell = None
                changed = True
            if self.giveup_solver and not self.giveup_solver.done:
                self.giveup_solver.step()
                if self.giveup_solver.done:
                    self.result = f"Solved by the Pi — {len(self.giveup_solver.path) - 1} moves from where you were"
                changed = True
            if self.status == "playing" and self.racer and not self.racer.done:
                self._racer_acc += dt * DIFFICULTY[self.difficulty]["race"]
                while self._racer_acc >= 1 and not self.racer.done:
                    self._racer_acc -= 1
                    self.racer.step()
                    changed = True
                if self.racer.done:
                    self._finish("lost", "The Pi got there first")
            if self.status == "playing" and self.ghosts:
                self._ghost_acc += dt * DIFFICULTY[self.difficulty]["ghost_speed"]
                while self._ghost_acc >= 1:
                    self._ghost_acc -= 1
                    self._move_ghosts()
                    changed = True
                self._check_ghosts()
            if changed:
                self._bump()

    def _move_ghosts(self):
        dist_to_player = self.maze.distances(self.player)
        for i, g in enumerate(self.ghosts):
            options = self.maze.neighbours(g)
            if dist_to_player.get(g, 99) <= CHASE_RANGE:
                nxt = min(options, key=lambda c: dist_to_player.get(c, 99))
            else:
                fwd = [c for c in options if c != self._ghost_prev[i]] or options
                nxt = self.rng.choice(fwd)
            self._ghost_prev[i] = g
            self.ghosts[i] = nxt

    def _check_ghosts(self):
        if self.status != "playing" or time.monotonic() < self.hurt_until:
            return
        if self.player in self.ghosts:
            self.lives -= 1
            self.hurt_until = time.monotonic() + HURT_SECS
            if self.lives <= 0:
                self._finish("lost", f"Caught! {self.score} pts")
            else:
                self.player = self.maze.start
                self.ghosts = list(self._ghost_spawn)
            self._bump()

    # ------------------------------------------------------------------
    def render_state(self) -> dict:
        """What the panel renderer needs."""
        with self.lock:
            gs = self.giveup_solver
            return {
                "player": self.player,
                "trail": [] if self.mode == "pacman" else list(self.trail),
                "dots": list(self.dots),
                "ghosts": list(self.ghosts),
                "racer": {"head": self.racer.head, "cells": dict(self.racer.cells)} if self.racer else None,
                "visible": self.visible if self.status not in ("won", "lost", "gaveup") else None,
                "seen": self.seen,
                "hint_cell": self.hint_cell,
                "hurt": time.monotonic() < self.hurt_until,
                "show_exit": self.mode != "pacman",
                "giveup": gs,
            }

    def to_json(self, include_maze=True) -> dict:
        with self.lock:
            out = {
                "version": self.version, "mode": self.mode, "size": self.size, "difficulty": self.difficulty,
                "status": self.status, "result": self.result, "rank": self.rank,
                "player": list(self.player), "moves": self.moves, "secs": round(self.elapsed(), 1),
                "hints": self.hints, "hint": list(self.hint_cell) if self.hint_cell else None,
                "lives": self.lives if self.mode == "pacman" else None,
                "score": self.score if self.mode == "pacman" else None,
                "dots": [list(c) for c in self.dots] if self.mode == "pacman" else [],
                "dots_left": len(self.dots),
                "ghosts": [list(g) for g in self.ghosts],
                "racer": list(self.racer.head) if self.racer else None,
                "trail": [] if self.mode == "pacman" else [list(c) for c in self.trail],
                "visible": [list(c) for c in self.visible] if self.visible is not None and self.status not in ("won", "lost", "gaveup") else None,
                "seen": [list(c) for c in self.seen] if self.mode == "fog" else None,
                "hurt": time.monotonic() < self.hurt_until,
            }
            if self.giveup_solver:
                out["solution"] = [list(c) for c in (self.giveup_solver.path or self.giveup_solver.trace or [])]
            if include_maze:
                out["maze"] = self.maze.to_json()
            return out
