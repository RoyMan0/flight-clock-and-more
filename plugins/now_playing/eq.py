"""
EQ styles for Now Playing — a spectrum-analyser look driven by the song's
tempo / energy / danceability / loudness / acousticness (features.py).

The Pi never hears the audio, so this is an estimate: the beat grid comes from
the song's real tempo, anchored to its playback position (so it stays steady
across polls), and each instrument layer is shaped by the song's numbers:
  kick   every beat, low bands          — stronger with energy
  snare  beats 2 and 4, mid bands       — stronger with energy
  hats   eighth notes, high bands       — stronger with danceability
  body   slow smoothed noise everywhere — more with energy; acoustic songs
         lean on the mids, drop the sub-bass and soften the hats
  level  overall height from loudness

Bars jump up and fall back at a fixed rate with peak caps, like a real analyser.

Styles:
  eq         segmented rainbow bars with peak caps
  eq_mirror  bars grow up and down from a centre line (a reflected spectrum)
  eq_wave    an oscilloscope line built from the levels, with fading trails
  eq_burst   the bands as spokes radiating from the centre, with a beat-pulsing core
Each one fills the panel, or, with the song info on (show_info), y0-22 above a
"Title - Artist" marquee in tom-thumb (y24-29) and the progress bar (y31).
render_style() draws any of them; visual_height() is the area they get.
"""

import colorsys
import math
import os
import random

from PIL import Image

from plugins.now_playing.features import with_defaults
from plugins.transit.render import _bdf_width, _parse_bdf

MATRIX_W, MATRIX_H = 64, 32
BANDS = 16
BAR_W, BAR_GAP = 3, 1                # 16 × 4 px = 64
BAR_Y = 31
COL_TRACK = (40, 40, 40)
COL_FILL = (0, 170, 0)
COL_TITLE = (0, 200, 0)
COL_ARTIST = (200, 200, 200)
COL_SEP = (90, 90, 90)
PEAK_HOLD = 6                        # frames a peak cap waits before falling
PEAK_FALL = 0.035                    # of full height per frame
BAR_FALL = 0.07
TEXT_Y = 24
TEXT_GAP = 20

_TINY = _parse_bdf(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "fonts", "tom-thumb.bdf")))
_TINY_H = 6
_TINY_BASELINE = 4                   # ascent 5 (rows 0-4), 1 px descender below


def _tiny_draw(img: Image.Image, x: int, text: str, colour: tuple):
    """tom-thumb glyphs have per-glyph boxes: sit each on the baseline (so lower case
    isn't drawn from the top of the line), descenders below it."""
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = _TINY.get(ord(ch))
        if g is None:
            cx += 4
            continue
        top = _TINY_BASELINE - g["bbh"] - g.get("yoff", 0) + 1
        bits = ((g["bbw"] + 7) // 8) * 8
        for ri, rv in enumerate(g["rows"]):
            for bi in range(g["bbw"]):
                if (rv >> (bits - 1 - bi)) & 1:
                    ox, oy = cx + g.get("xoff", 0) + bi, top + ri
                    if 0 <= ox < iw and 0 <= oy < ih:
                        px[ox, oy] = colour
        cx += g["dw"]


def _pulse(phase: float, sharp: float) -> float:
    return math.exp(-phase * sharp)


class Analyzer:
    """Per-band levels (0..1) for a song; call levels(t) once per frame."""

    def __init__(self, features=None, seed: int = 0):
        self.f = with_defaults(features)
        self.rng = random.Random(seed)
        self.noise = [0.0] * BANDS
        self.shown = [0.0] * BANDS
        self.peaks = [0.0] * BANDS
        self.hold = [0] * BANDS
        f = self.f
        self.energy = max(0.0, min(1.0, f["energy"]))
        self.dance = max(0.0, min(1.0, f["danceability"]))
        self.acoustic = max(0.0, min(1.0, f["acousticness"]))
        tempo = f["tempo"] or 110.0
        while tempo > 180:                        # analysers read double-time; keep it musical
            tempo /= 2
        while tempo < 60:
            tempo *= 2
        self.bps = tempo / 60.0
        # -20 dB (quiet) … -4 dB (loud master) → 0.55 … 1.0
        self.level = 0.55 + 0.45 * max(0.0, min(1.0, (f["loudness"] + 20) / 16))
        # Resting spectrum: a gentle downward tilt; acoustic songs lose sub-bass and air
        self.base = []
        for b in range(BANDS):
            x = b / (BANDS - 1)
            tilt = 0.55 - 0.3 * x
            mid_bump = 0.18 * self.acoustic * math.exp(-((x - 0.45) ** 2) / 0.04)
            edge_cut = self.acoustic * (0.25 * math.exp(-x * 8) + 0.15 * x ** 3)
            self.base.append(max(0.08, tilt + mid_bump - edge_cut))

    def target(self, t: float) -> list:
        beat = t * self.bps
        ph = beat % 1.0
        beat_n = int(beat)
        ph8 = (beat * 2) % 1.0
        kick = _pulse(ph, 7) * (0.45 + 0.55 * self.energy) * (1 - 0.5 * self.acoustic)
        snare = _pulse(ph, 9) * (0.3 + 0.5 * self.energy) if beat_n % 2 == 1 else 0.0
        hat = _pulse(ph8, 14) * (0.15 + 0.6 * self.dance) * (1 - 0.6 * self.acoustic)
        out = []
        for b in range(BANDS):
            x = b / (BANDS - 1)
            # Smoothed random movement — the "everything else" of the mix
            self.noise[b] += (self.rng.random() - 0.5) * 0.4
            self.noise[b] *= 0.82
            v = self.base[b] * (0.65 + 0.35 * self.energy)
            v += kick * math.exp(-((x - 0.08) ** 2) / 0.02)
            v += snare * math.exp(-((x - 0.42) ** 2) / 0.03)
            v += hat * math.exp(-((x - 0.85) ** 2) / 0.03)
            v += self.noise[b] * (0.25 + 0.35 * self.energy)
            # Soft-knee compression, like an analyser's log scale: fuller bars, never clipped flat
            out.append(1.0 - math.exp(-2.4 * max(0.0, v * self.level)))
        return out

    def levels(self, t: float, playing: bool = True) -> list:
        tgt = self.target(t) if playing else [0.0] * BANDS
        for b in range(BANDS):
            self.shown[b] = tgt[b] if tgt[b] > self.shown[b] else max(tgt[b], self.shown[b] - BAR_FALL)
            if self.shown[b] >= self.peaks[b]:
                self.peaks[b], self.hold[b] = self.shown[b], PEAK_HOLD
            elif self.hold[b] > 0:
                self.hold[b] -= 1
            else:
                self.peaks[b] = max(0.0, self.peaks[b] - PEAK_FALL)
        return list(self.shown)


def _bar_colour(b: int, row_frac: float, hue_shift: float) -> tuple:
    """Rainbow across the bands; each bar brightens toward its top."""
    h = (b / BANDS * 0.85 + hue_shift) % 1.0
    r, g, bl = colorsys.hsv_to_rgb(h, 1.0, 0.55 + 0.45 * row_frac)
    return int(r * 255), int(g * 255), int(bl * 255)


def draw_bars(img: Image.Image, an: Analyzer, height: int, hue_shift: float = 0.0):
    """Segmented bars (1 px lit, 1 px gap) from y=height-1 upward, with peak caps."""
    px = img.load()
    segs = (height + 1) // 2
    for b in range(BANDS):
        x0 = b * (BAR_W + BAR_GAP)
        lit = int(round(an.shown[b] * segs))
        for s in range(lit):
            y = height - 1 - s * 2
            c = _bar_colour(b, s / max(1, segs - 1), hue_shift)
            for dx in range(BAR_W):
                px[x0 + dx, y] = c
        peak = int(round(an.peaks[b] * segs))
        if peak > lit and peak > 0:
            y = height - 1 - (peak - 1) * 2
            for dx in range(BAR_W):
                px[x0 + dx, y] = (235, 235, 235)


def draw_progress(img: Image.Image, progress):
    if progress is None:
        return
    filled = int(round(max(0.0, min(1.0, progress)) * MATRIX_W))
    px = img.load()
    for x in range(MATRIX_W):
        px[x, BAR_Y] = COL_FILL if x < filled else COL_TRACK


def _ascii(text: str) -> str:
    table = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "•": "-"}
    out = "".join(table.get(c, c) for c in text or "")
    return "".join(c for c in out if ord(c) in _TINY or c == " ")


class TinyMarquee:
    """'Title · Artist' in tom-thumb: static when it fits, otherwise scrolls left continuously."""

    def __init__(self, track: dict):
        title, artist = _ascii(track.get("title", "")), _ascii(track.get("artist", ""))
        parts = [(title, COL_TITLE)] + ([(" - ", COL_SEP), (artist, COL_ARTIST)] if artist else [])
        self.text_w = sum(_bdf_width(t, _TINY) for t, _ in parts)
        self.scrolls = self.text_w > MATRIX_W
        strip_w = self.text_w + (TEXT_GAP + self.text_w if self.scrolls else 0)
        self.strip = Image.new("RGB", (max(1, strip_w), _TINY_H), (0, 0, 0))
        for start in ((0, self.text_w + TEXT_GAP) if self.scrolls else (0,)):
            x = start
            for t, c in parts:
                _tiny_draw(self.strip, x, t, c)
                x += _bdf_width(t, _TINY)
        self.offset = 0.0
        self.pause = 15

    def reset(self):
        self.offset, self.pause = 0.0, 15

    def tick(self, speed: float):
        if not self.scrolls:
            return
        if self.pause > 0:
            self.pause -= 1
            return
        self.offset = (self.offset + speed) % (self.text_w + TEXT_GAP)

    def paste_into(self, img: Image.Image, y: int):
        if not self.scrolls:
            img.paste(self.strip, ((MATRIX_W - self.text_w) // 2, y))
            return
        o = int(self.offset)
        img.paste(self.strip.crop((o, 0, o + MATRIX_W, _TINY_H)), (0, y))


# ---- more styles ------------------------------------------------------------------

def visual_height(progress, marquee) -> int:
    """Rows the visual gets: above the marquee, above the progress bar, or the whole panel."""
    if marquee is not None:
        return TEXT_Y - 1
    return BAR_Y - 1 if progress is not None else MATRIX_H


def _finish(img: Image.Image, progress, marquee):
    if marquee is not None:
        marquee.paste_into(img, TEXT_Y)
    draw_progress(img, progress)
    return img


def render_mirror(an: Analyzer, progress, hue_shift: float = 0.0, marquee: "TinyMarquee" = None) -> Image.Image:
    """Bars from a centre line up and down; the lower half is a dimmer reflection."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    px = img.load()
    hgt = visual_height(progress, marquee)
    mid = (hgt - 1) // 2
    half, below = mid + 1, hgt - 1 - mid
    for b in range(BANDS):
        x0 = b * (BAR_W + BAR_GAP)
        h = int(round(an.shown[b] * half))
        for s in range(h):
            frac = s / max(1, half - 1)
            hue = (0.5 + 0.35 * frac + hue_shift) % 1.0          # cyan → violet → pink
            r, g, bl = colorsys.hsv_to_rgb(hue, 0.85, 0.6 + 0.4 * frac)
            up = (int(r * 255), int(g * 255), int(bl * 255))
            down = tuple(v * 2 // 5 for v in up)
            for dx in range(BAR_W):
                px[x0 + dx, mid - s] = up
                if s < below:
                    px[x0 + dx, mid + 1 + s] = down
        pk = int(round(an.peaks[b] * half))
        if pk > h and pk > 0:
            for dx in range(BAR_W):
                px[x0 + dx, mid - pk + 1] = (235, 235, 235)
    return _finish(img, progress, marquee)


def wave_samples(an: Analyzer, t: float, n: int) -> list:
    """A waveform (-1..1, n samples) made from the band levels: low bands are slow, wide
    swings, high bands fast ripples; phases drift with time so the line keeps moving."""
    # Per band: frequency across the line, a fixed phase and a drift speed. Phases spread
    # by the golden angle and uneven speeds — linear ones line up into a single spike.
    waves = [(0.5 + 0.8 * bb ** 1.15, bb * bb * 2.39996, 0.8 + 2.4 * ((bb * 0.618034) % 1.0))
             for bb in range(BANDS)]
    out = []
    for i in range(n):
        x = i / (n - 1)
        v = 0.0
        for b, (f, ph, speed) in enumerate(waves):
            v += an.shown[b] * math.sin(2 * math.pi * f * x + ph + t * speed) / (1 + b * 0.2)
        out.append(v)
    peak = max(1.5, max(abs(v) for v in out))
    return [v / peak for v in out]


def render_wave(an: Analyzer, t: float, progress, trail: Image.Image, hue_shift: float = 0.0,
                marquee: "TinyMarquee" = None):
    """Oscilloscope with phosphor trails: the previous frame fades, the new line is drawn on
    top. Returns (frame, trail) — keep the trail for the next call."""
    hgt = visual_height(progress, marquee)
    if trail is None or trail.size != (MATRIX_W, hgt):
        trail = Image.new("RGB", (MATRIX_W, hgt))
    else:
        trail = Image.eval(trail, lambda v: v * 3 // 5)
    px = trail.load()
    amp = (hgt / 2 - 2) * (0.35 + 0.65 * sum(an.shown) / BANDS)
    ys = [(hgt - 1) / 2 + amp * v for v in wave_samples(an, t, MATRIX_W)]
    r, g, b = colorsys.hsv_to_rgb((0.36 + hue_shift) % 1.0, 0.7, 1.0)
    col = (int(r * 255), int(g * 255), int(b * 255))
    prev = None
    for x, yf in enumerate(ys):
        y = int(round(max(0, min(hgt - 1, yf))))
        lo, hi = (y, y) if prev is None else (min(prev, y), max(prev, y))
        for yy in range(lo, hi + 1):                      # join steep steps so the line never breaks
            px[x, yy] = col
        prev = y
    frame = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    frame.paste(trail, (0, 0))
    return _finish(frame, progress, marquee), trail


_SPOKES = 32


def render_burst(an: Analyzer, t: float, progress, hue_shift: float = 0.0,
                 marquee: "TinyMarquee" = None) -> Image.Image:
    """32 spokes (the 16 bands, mirrored left/right) from the centre; length = level."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    px = img.load()
    hgt = visual_height(progress, marquee)
    cx, cy, squash = 31.5, (hgt - 1) / 2, 0.55 * hgt / 30
    spin = t * 0.15
    for k in range(_SPOKES):
        q = k % (_SPOKES // 2)                          # each half repeats the bands …
        band = q if q < BANDS // 2 else _SPOKES // 2 - 1 - q   # … low in the middle, high at the sides
        band = band * 2 + (k // (_SPOKES // 2))         # use all 16 bands across the two halves
        level = an.shown[min(BANDS - 1, band)]
        ang = 2 * math.pi * k / _SPOKES + spin
        ca, sa = math.cos(ang), math.sin(ang)
        reach = 4 + level * 38
        r = 3.0
        while r <= reach:
            x, y = int(round(cx + ca * r)), int(round(cy + sa * r * squash))
            if 0 <= x < MATRIX_W and 0 <= y < hgt:
                fade = 1 - 0.6 * (r / 31)
                cr, cg, cb = colorsys.hsv_to_rgb((band / BANDS * 0.8 + hue_shift) % 1.0, 1.0, fade)
                px[x, y] = (int(cr * 255), int(cg * 255), int(cb * 255))
            r += 0.7
    core = 1 + int(2 * an.shown[0])                         # the kick pulses the core
    for dy in range(-core, core + 1):
        for dx in range(-core, core + 1):
            if dx * dx + dy * dy <= core * core:
                px[int(cx) + dx, int(round(cy)) + dy] = (255, 255, 255)
    return _finish(img, progress, marquee)


def render_eq(an: Analyzer, progress, hue_shift: float = 0.0, marquee: TinyMarquee = None) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw_bars(img, an, visual_height(progress, marquee), hue_shift)
    return _finish(img, progress, marquee)


def render_style(style: str, an: Analyzer, t: float, progress, hue_shift: float = 0.0,
                 marquee: TinyMarquee = None, trail=None):
    """Any EQ style (anything else, e.g. the desktop's viz_* styles, draws as "eq").
    Returns (frame, trail); only eq_wave uses the trail — keep it for the next call."""
    if style == "eq_mirror":
        return render_mirror(an, progress, hue_shift, marquee), None
    if style == "eq_wave":
        return render_wave(an, t, progress, trail, hue_shift, marquee)
    if style == "eq_burst":
        return render_burst(an, t, progress, hue_shift, marquee), None
    return render_eq(an, progress, hue_shift, marquee), None
