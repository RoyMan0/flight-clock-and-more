"""
Maze rendering (64×32). Walls in the theme colour on black; solver routes in
the theme's route palette (a new colour after every dead end, failed branches
dimmed); flood fill as a colour gradient by distance; start/exit/head markers;
a rainbow wave along the winning path and a stats card when solved.
Play-mode extras: player, Pi racer, ghosts, dots and fog of war.
"""

import colorsys
import random

from PIL import Image

from plugins.maze.generate import DIRS, E, S, Layout
from plugins.transit.render import _FONT, _bdf_draw, _bdf_width

MATRIX_W, MATRIX_H = 64, 32

THEMES = {
    "neon": {
        "wall": (120, 40, 210), "start": (0, 255, 90), "exit": (255, 40, 40), "head": (255, 255, 255),
        "player": (255, 255, 255), "racer": (0, 200, 255), "dot": (120, 120, 140),
        "routes": [(0, 255, 200), (255, 40, 160), (255, 220, 0), (0, 150, 255),
                   (255, 110, 0), (170, 255, 0), (255, 0, 90), (200, 120, 255)],
    },
    "pacman": {
        "wall": (33, 33, 222), "start": (0, 255, 90), "exit": (255, 60, 60), "head": (255, 230, 0),
        "player": (255, 230, 0), "racer": (0, 230, 255), "dot": (255, 190, 160),
        "routes": [(255, 230, 0), (255, 140, 0), (255, 100, 190), (0, 230, 255),
                   (255, 60, 60), (150, 255, 100)],
    },
    "retro": {
        "wall": (0, 110, 45), "start": (180, 255, 180), "exit": (255, 200, 60), "head": (230, 255, 230),
        "player": (230, 255, 230), "racer": (255, 200, 60), "dot": (60, 140, 70),
        "routes": [(110, 255, 110), (255, 200, 60), (0, 255, 200), (200, 255, 80),
                   (255, 130, 40), (90, 200, 255)],
    },
}
THEME_NAMES = tuple(THEMES)
GHOST_COLOURS = [(255, 0, 0), (255, 140, 220), (0, 240, 240), (255, 170, 60)]


def pick_theme(name: str, rng: random.Random = None) -> str:
    if name in THEMES:
        return name
    return (rng or random).choice(THEME_NAMES)


def _dim(c, f):
    return tuple(int(v * f) for v in c)


def _hsv(h, s=1.0, v=1.0):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


_wall_cache = {}            # (maze seed, style, size, colour) → walls-only image


class Canvas:
    """Paints cells and the passages between them for one layout."""

    def __init__(self, layout: Layout, maze, base: Image.Image = None):
        self.L, self.maze = layout, maze
        self.img = base.copy() if base is not None else Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
        self.px = self.img.load()

    @classmethod
    def with_walls(cls, layout, maze, colour):
        """Canvas pre-filled with the maze walls (cached per maze — the walls never change)."""
        key = (maze.seed, maze.style, layout.path, colour)
        base = _wall_cache.get(key)
        if base is None:
            cv = cls(layout, maze)
            cv.walls(colour)
            base = cv.img
            if len(_wall_cache) > 8:
                _wall_cache.clear()
            _wall_cache[key] = base
        return cls(layout, maze, base)

    def rect(self, x0, y0, w, h, colour):
        for y in range(y0, y0 + h):
            for x in range(x0, x0 + w):
                if 0 <= x < MATRIX_W and 0 <= y < MATRIX_H:
                    self.px[x, y] = colour

    def cell(self, cell, colour, inset=0):
        x, y = self.L.cell_origin(*cell)
        p = self.L.path
        if inset and p > 2:
            self.rect(x + inset, y + inset, p - 2 * inset, p - 2 * inset, colour)
        else:
            self.rect(x, y, p, p, colour)

    def passage(self, a, b, colour):
        """Fill the wall gap between two adjacent, connected cells."""
        (ax, ay), (bx, by) = a, b
        x, y = self.L.cell_origin(min(ax, bx), min(ay, by))
        p, w = self.L.path, self.L.wall
        if ay == by:
            self.rect(x + p, y, w, p, colour)
        else:
            self.rect(x, y + p, p, w, colour)

    def walls(self, colour, visible=None):
        """Draw every wall pixel (everything that isn't a cell or an open passage)."""
        L, m = self.L, self.maze
        self.rect(L.ox, L.oy, L.px_w, L.px_h, colour)
        floor = (0, 0, 0)
        for y in range(m.rows):
            for x in range(m.cols):
                if visible is not None and (x, y) not in visible:
                    continue
                self.cell((x, y), floor)
                if m.open(x, y, E):
                    self.passage((x, y), (x + 1, y), floor)
                if m.open(x, y, S):
                    self.passage((x, y), (x, y + 1), floor)


def _route_colours(solver, theme):
    """Solver cell states → {cell: rgb}."""
    t = THEMES[theme]
    pal = t["routes"]
    out = {}
    for cell, (idx, state) in solver.cells.items():
        if isinstance(state, int):                        # flood distance
            out[cell] = _hsv(state / 40.0, 0.85, 0.55)
        elif state == "dead":
            out[cell] = _dim(pal[idx % len(pal)], 0.28)
        else:
            out[cell] = pal[idx % len(pal)]
    trace = getattr(solver, "trace", None)
    if trace:
        for c in trace:
            out[c] = (255, 255, 255)
    return out


def _paint_colours(cv, maze, colours, states=None):
    for cell, rgb in colours.items():
        cv.cell(cell, rgb)
    for cell, rgb in colours.items():
        for n in maze.neighbours(cell):
            if n in colours and (n[0] > cell[0] or n[1] > cell[1]):
                # Passage takes the dimmer (failed) colour, or the newer route's
                other = colours[n]
                cv.passage(cell, n, min(rgb, other, key=sum) if states and (
                    states.get(cell) == "dead" or states.get(n) == "dead") else other)


def render_solving(layout, maze, theme, solver, frame: int) -> Image.Image:
    t = THEMES[theme]
    cv = Canvas.with_walls(layout, maze, t["wall"])
    states = {c: s for c, (_i, s) in solver.cells.items()}
    _paint_colours(cv, maze, _route_colours(solver, theme), states)
    cv.cell(maze.start, t["start"])
    cv.cell(maze.exit, t["exit"] if (frame // 5) % 2 == 0 else _dim(t["exit"], 0.45))
    cv.cell(solver.head, t["head"])
    return cv.img


def render_celebration(layout, maze, theme, path, frame: int) -> Image.Image:
    t = THEMES[theme]
    cv = Canvas.with_walls(layout, maze, _dim(t["wall"], 0.6))
    colours = {c: _hsv(i / 24.0 - frame / 15.0) for i, c in enumerate(path)}
    _paint_colours(cv, maze, colours)
    return cv.img


def render_stats(base: Image.Image, lines: list, accent) -> Image.Image:
    """Darkened maze with centred text lines (first line in the accent colour)."""
    img = Image.eval(base, lambda v: v // 5)
    n = len(lines)
    top = (MATRIX_H - (n * 8 - 2)) // 2
    for i, text in enumerate(lines):
        w = _bdf_width(text, _FONT)
        _bdf_draw(img, (MATRIX_W - w) // 2, top + i * 8, text, _FONT, accent if i == 0 else (235, 235, 235))
    return img


def render_play(layout, maze, theme, state: dict, frame: int) -> Image.Image:
    """Play-mode frame from a game state dict (see game.py)."""
    t = THEMES[theme]
    cv = Canvas(layout, maze)
    fog = state.get("visible")                           # set of cells, or None
    seen = state.get("seen")
    if fog is not None:
        cv.walls(_dim(t["wall"], 0.35), visible=seen)    # remembered cells, faint
        cv_vis = Canvas(layout, maze)
        cv_vis.walls(t["wall"], visible=fog)
        for c in fog:                                     # overlay the lit area
            x, y = layout.cell_origin(*c)
            for yy in range(y - layout.wall, y + layout.path + layout.wall):
                for xx in range(x - layout.wall, x + layout.path + layout.wall):
                    if 0 <= xx < MATRIX_W and 0 <= yy < MATRIX_H:
                        cv.px[xx, yy] = cv_vis.px[xx, yy]
    else:
        cv = Canvas.with_walls(layout, maze, t["wall"])

    def visible(c):
        return fog is None or c in fog

    for c in state.get("trail", []):
        if visible(c):
            cv.cell(c, _dim(t["player"], 0.22))
    for c in state.get("dots", []):
        if visible(c):
            cv.cell(c, t["dot"], inset=1) if layout.path > 2 else _dot(cv, layout, c, t["dot"])
    racer = state.get("racer")
    if racer:
        for c, (idx, st) in racer.get("cells", {}).items():
            if visible(c):
                cv.cell(c, _dim(t["racer"], 0.25 if st == "dead" else 0.45))
    if state.get("show_exit", True) and visible(maze.exit):
        cv.cell(maze.exit, t["exit"] if (frame // 5) % 2 == 0 else _dim(t["exit"], 0.45))
    if racer and visible(tuple(racer["head"])):
        cv.cell(tuple(racer["head"]), t["racer"])
    for i, g in enumerate(state.get("ghosts", [])):
        if visible(tuple(g)):
            cv.cell(tuple(g), GHOST_COLOURS[i % len(GHOST_COLOURS)])
    hint = state.get("hint_cell")
    if hint and (frame // 3) % 2 == 0:
        cv.cell(tuple(hint), (255, 255, 255))
    p = tuple(state["player"])
    flash = state.get("hurt") and (frame // 2) % 2 == 0
    cv.cell(p, (255, 0, 0) if flash else t["player"])
    return cv.img


def _dot(cv, layout, c, colour):
    x, y = layout.cell_origin(*c)
    m = layout.path // 2
    cv.px[x + m, y + m] = colour
