"""
Maze solvers as step machines — call step() once per tick; the plugin calls it
`solve_speed` times per frame, so progress pauses with the rotation and resumes.

Every solver exposes:
  done        True once the exit is reached (and the final path is known)
  path        the winning route (list of cells) once done
  head        the cell the solver is on now
  dead_ends   how many dead ends it has hit
  steps       how many steps it has taken
  cells       {cell: (colour_index, state)} — state is "route", "dead" or a
              flood distance (int); the renderer turns these into colours

  explorer  depth-first: walks a route; at a dead end it backtracks to the
            last junction and the NEXT route gets a new colour. Abandoned
            branches stay on screen, dimmed, in the colour they had.
  wall      right hand on the wall; the trail changes colour at every dead-end
            turnaround. (In mazes with loops a wall follower can circle forever,
            so if it repeats itself it hands over to the explorer.)
  flood     breadth-first: the whole frontier spreads one layer per step like
            water, then the shortest path is traced back from the exit.
"""

import random

from plugins.maze.generate import DIRS, E, N, S, W

SOLVERS = ("explorer", "wall", "flood")


class Explorer:
    name = "explorer"

    def __init__(self, maze, start=None, seed=0):
        self.maze = maze
        self.rng = random.Random(seed)
        start = start or maze.start
        self.stack = [start]
        self.visited = {start}
        self.colour = 0
        self.backtracking = False
        self.cells = {start: (0, "route")}
        self.dead_ends = 0
        self.steps = 0
        self.done = start == maze.exit
        self.path = list(self.stack) if self.done else []

    @property
    def head(self):
        return self.stack[-1] if self.stack else self.maze.start

    def step(self):
        if self.done or not self.stack:
            return
        cur = self.stack[-1]
        options = [n for n in self.maze.neighbours(cur) if n not in self.visited]
        self.steps += 1
        if options:
            if self.backtracking:              # leaving a junction on a new route
                self.colour += 1
                self.backtracking = False
            nxt = self.rng.choice(options)
            self.visited.add(nxt)
            self.stack.append(nxt)
            self.cells[nxt] = (self.colour, "route")
            if nxt == self.maze.exit:
                self.done = True
                self.path = list(self.stack)
        else:
            if not self.backtracking:           # just ran into a dead end
                self.dead_ends += 1
                self.backtracking = True
            gone = self.stack.pop()
            self.cells[gone] = (self.cells[gone][0], "dead")


class WallFollower:
    name = "wall"
    # Headings in clockwise order; "right" of heading i is i+1
    _ORDER = (N, E, S, W)

    def __init__(self, maze, start=None, seed=0):
        self.maze = maze
        self.pos = start or maze.start
        self.heading = 1                        # facing east
        self.colour = 0
        self.cells = {self.pos: (0, "route")}
        self.dead_ends = 0
        self.steps = 0
        self.seen_states = set()
        self.fallback = None                     # Explorer if we start circling
        self.done = self.pos == maze.exit
        self.path = [self.pos] if self.done else []

    @property
    def head(self):
        return self.fallback.head if self.fallback else self.pos

    def step(self):
        if self.done:
            return
        if self.fallback:
            self.fallback.step()
            self.cells.update(self.fallback.cells)
            self.steps += 1
            self.dead_ends = self._fallback_base + self.fallback.dead_ends
            if self.fallback.done:
                self.done = True
                self.path = self.maze.shortest_path(self.maze.start, self.maze.exit)
            return
        state = (self.pos, self.heading)
        if state in self.seen_states:            # circling a loop — hand over
            self.fallback = Explorer(self.maze, self.pos)
            self.fallback.colour = self.colour + 1
            self._fallback_base = self.dead_ends
            return
        self.seen_states.add(state)
        for turn in (1, 0, 3, 2):                # right, straight, left, back
            h = (self.heading + turn) % 4
            nxt = self.maze.move(self.pos, self._ORDER[h])
            if nxt is not None:
                if turn == 2:                   # had to turn around: dead end
                    self.dead_ends += 1
                    self.colour += 1
                self.heading = h
                self.pos = nxt
                self.cells[nxt] = (self.colour, "route")
                break
        self.steps += 1
        if self.pos == self.maze.exit:
            self.done = True
            self.path = self.maze.shortest_path(self.maze.start, self.maze.exit)


class Flood:
    name = "flood"

    def __init__(self, maze, start=None, seed=0):
        self.maze = maze
        start = start or maze.start
        self.dist = {start: 0}
        self.prev = {start: None}
        self.frontier = [start]
        self.cells = {start: (0, 0)}
        self.dead_ends = 0
        self.steps = 0
        self.trace = None                        # cells of the traced path so far
        self._trace_src = None
        self.done = False
        self.path = []

    @property
    def head(self):
        if self.trace:
            return self.trace[-1]
        return self.frontier[0] if self.frontier else self.maze.exit

    def step(self):
        if self.done:
            return
        self.steps += 1
        if self.trace is None:
            nxt = []
            for c in self.frontier:
                new = [n for n in self.maze.neighbours(c) if n not in self.dist]
                if not new and len(self.maze.neighbours(c)) == 1 and c != self.maze.start:
                    self.dead_ends += 1
                for n in new:
                    self.dist[n] = self.dist[c] + 1
                    self.prev[n] = c
                    self.cells[n] = (0, self.dist[n])
                    nxt.append(n)
            self.frontier = nxt
            if self.maze.exit in self.dist:      # water reached the exit — trace back
                route, c = [], self.maze.exit
                while c is not None:
                    route.append(c)
                    c = self.prev[c]
                self._trace_src = route          # exit → start
                self.trace = []
            elif not nxt:
                self.done = True                 # unreachable (shouldn't happen)
            return
        # Trace the shortest path back, one cell per step
        self.trace.append(self._trace_src[len(self.trace)])
        if len(self.trace) == len(self._trace_src):
            self.done = True
            self.path = self.trace[::-1]


def make_solver(name: str, maze, start=None, seed=0):
    cls = {"explorer": Explorer, "wall": WallFollower, "flood": Flood}.get(name, Explorer)
    return cls(maze, start, seed)
