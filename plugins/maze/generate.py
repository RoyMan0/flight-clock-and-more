"""
Maze generation for the 64×32 panel.

A maze is a grid of cells; each cell stores which of its four sides are open
(bitmask N/E/S/W). The size setting picks the corridor and wall thickness:

  S  1 px paths, 1 px walls → 31×15 cells (63×31 px)
  M  2 px paths, 1 px walls → 21×10 cells (64×31 px)
  L  3 px paths, 1 px walls → 15×7  cells (61×29 px)

Styles:
  winding  iterative recursive backtracker — long corridors, few branches
  branchy  randomised Prim's — lots of short dead ends
  loops    backtracker, then ~30% of dead ends knocked through ("braided"),
           so there are several routes (Pac-Man mode always uses this)

The start is the top-left cell; the exit is the cell farthest from it, so
every maze has a long route.
"""

import random
from collections import deque

MATRIX_W, MATRIX_H = 64, 32
N, E, S, W = 1, 2, 4, 8
DIRS = {N: (0, -1), E: (1, 0), S: (0, 1), W: (-1, 0)}
OPPOSITE = {N: S, S: N, E: W, W: E}
DIR_NAMES = {"up": N, "right": E, "down": S, "left": W}

SIZES = {"S": (1, 1), "M": (2, 1), "L": (3, 1)}     # (path px, wall px)
STYLES = ("winding", "branchy", "loops")


class Layout:
    """Pixel geometry for a size setting."""

    def __init__(self, size: str):
        self.path, self.wall = SIZES.get(size, SIZES["M"])
        self.pitch = self.path + self.wall
        self.cols = (MATRIX_W - self.wall) // self.pitch
        self.rows = (MATRIX_H - self.wall) // self.pitch
        self.px_w = self.cols * self.pitch + self.wall
        self.px_h = self.rows * self.pitch + self.wall
        self.ox = (MATRIX_W - self.px_w) // 2
        self.oy = (MATRIX_H - self.px_h) // 2

    def cell_origin(self, x: int, y: int):
        """Top-left pixel of a cell's path area."""
        return self.ox + self.wall + x * self.pitch, self.oy + self.wall + y * self.pitch


class Maze:
    def __init__(self, cols: int, rows: int, cells: list, seed: int, style: str):
        self.cols, self.rows = cols, rows
        self.cells = cells                    # index y*cols+x → open-sides bitmask
        self.seed, self.style = seed, style
        self.start = (0, 0)
        self.exit = self._farthest(self.start)

    def open(self, x, y, d) -> bool:
        return bool(self.cells[y * self.cols + x] & d)

    def neighbours(self, cell):
        """Cells reachable in one move from `cell`."""
        x, y = cell
        out = []
        for d, (dx, dy) in DIRS.items():
            if self.open(x, y, d):
                out.append((x + dx, y + dy))
        return out

    def move(self, cell, d):
        """Cell reached by moving direction d, or None if a wall is in the way."""
        x, y = cell
        if not self.open(x, y, d):
            return None
        dx, dy = DIRS[d]
        return (x + dx, y + dy)

    def distances(self, origin):
        dist = {origin: 0}
        q = deque([origin])
        while q:
            c = q.popleft()
            for n in self.neighbours(c):
                if n not in dist:
                    dist[n] = dist[c] + 1
                    q.append(n)
        return dist

    def shortest_path(self, a, b):
        prev = {a: None}
        q = deque([a])
        while q:
            c = q.popleft()
            if c == b:
                break
            for n in self.neighbours(c):
                if n not in prev:
                    prev[n] = c
                    q.append(n)
        if b not in prev:
            return []
        path, c = [], b
        while c is not None:
            path.append(c)
            c = prev[c]
        return path[::-1]

    def _farthest(self, origin):
        dist = self.distances(origin)
        return max(dist, key=lambda c: (dist[c], c[1], c[0]))

    def dead_ends(self):
        return [(x, y) for y in range(self.rows) for x in range(self.cols)
                if bin(self.cells[y * self.cols + x]).count("1") == 1]

    def to_json(self) -> dict:
        return {"cols": self.cols, "rows": self.rows, "cells": self.cells,
                "start": list(self.start), "exit": list(self.exit), "style": self.style}


def _carve(cells, cols, x, y, d):
    dx, dy = DIRS[d]
    cells[y * cols + x] |= d
    cells[(y + dy) * cols + (x + dx)] |= OPPOSITE[d]


def _backtracker(cols, rows, rng):
    cells = [0] * (cols * rows)
    seen = {(0, 0)}
    stack = [(0, 0)]
    while stack:
        x, y = stack[-1]
        options = [d for d, (dx, dy) in DIRS.items()
                   if 0 <= x + dx < cols and 0 <= y + dy < rows and (x + dx, y + dy) not in seen]
        if not options:
            stack.pop()
            continue
        d = rng.choice(options)
        _carve(cells, cols, x, y, d)
        nxt = (x + DIRS[d][0], y + DIRS[d][1])
        seen.add(nxt)
        stack.append(nxt)
    return cells


def _prim(cols, rows, rng):
    cells = [0] * (cols * rows)
    inside = {(0, 0)}
    frontier = []

    def add_frontier(x, y):
        for d, (dx, dy) in DIRS.items():
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows and (nx, ny) not in inside:
                frontier.append((x, y, d))

    add_frontier(0, 0)
    while frontier:
        i = rng.randrange(len(frontier))
        frontier[i], frontier[-1] = frontier[-1], frontier[i]
        x, y, d = frontier.pop()
        nx, ny = x + DIRS[d][0], y + DIRS[d][1]
        if (nx, ny) in inside:
            continue
        _carve(cells, cols, x, y, d)
        inside.add((nx, ny))
        add_frontier(nx, ny)
    return cells


def _braid(cells, cols, rows, rng, fraction=0.3):
    """Knock through a fraction of dead ends so the maze has loops."""
    dead = [(x, y) for y in range(rows) for x in range(cols)
            if bin(cells[y * cols + x]).count("1") == 1]
    rng.shuffle(dead)
    for x, y in dead[:int(len(dead) * fraction)]:
        if bin(cells[y * cols + x]).count("1") != 1:
            continue                                   # already opened by an earlier knock-through
        walls = [d for d, (dx, dy) in DIRS.items()
                 if not cells[y * cols + x] & d and 0 <= x + dx < cols and 0 <= y + dy < rows]
        if walls:
            # Prefer joining another dead end, which removes two at once
            walls.sort(key=lambda d: bin(cells[(y + DIRS[d][1]) * cols + x + DIRS[d][0]]).count("1") != 1)
            _carve(cells, cols, x, y, walls[0] if rng.random() < 0.7 else rng.choice(walls))
    return cells


def generate(size: str = "M", style: str = "random", seed: int = None) -> Maze:
    layout = Layout(size)
    seed = seed if seed is not None else random.randrange(1 << 30)
    rng = random.Random(seed)
    if style not in STYLES:
        style = rng.choice(STYLES)
    if style == "branchy":
        cells = _prim(layout.cols, layout.rows, rng)
    else:
        cells = _backtracker(layout.cols, layout.rows, rng)
        if style == "loops":
            cells = _braid(cells, layout.cols, layout.rows, rng)
    return Maze(layout.cols, layout.rows, cells, seed, style)
