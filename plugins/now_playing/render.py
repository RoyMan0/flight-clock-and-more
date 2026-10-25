"""
Now Playing rendering (64×32, 5x7 BDF font):

  y0-6    song title — green, marquee-scrolls when wider than the panel
  y8-29   22×22 artwork  |  x24-63: artist (white) / album (grey), each scrolls if needed
  y31     progress bar (omitted when the source has no duration, e.g. Last.fm)
"""

import io
import os

from PIL import Image, ImageEnhance

from plugins.transit.render import _bdf_draw, _bdf_width, _parse_bdf

MATRIX_W = 64
MATRIX_H = 32

ART_SIZE = 22
ART_X, ART_Y = 0, 8
TEXT_X = 24
TEXT_W = MATRIX_W - TEXT_X           # 40 px
TITLE_Y = 0
ARTIST_Y = 11
ALBUM_Y = 20
BAR_Y = 31

COL_TITLE  = (0, 200, 0)
COL_ARTIST = (230, 230, 230)
COL_ALBUM  = (150, 150, 150)
COL_TRACK  = (40, 40, 40)
COL_FILL   = (0, 170, 0)
COL_NOTE   = (90, 90, 90)

MARQUEE_GAP = 16                     # px between the end of a line and its repeat
MARQUEE_PAUSE_FRAMES = 15            # ~1.5 s at 10 fps before a line starts scrolling

_FONT = _parse_bdf(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "fonts", "5x7.bdf")))
_FONT_H = 7


def _ascii(text: str) -> str:
    """The BDF fonts are Latin-1; swap common typography and drop what can't render."""
    table = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "•": "-"}
    out = "".join(table.get(c, c) for c in text or "")
    return "".join(c for c in out if ord(c) in _FONT or c == " ")


class Marquee:
    """One line of text: static when it fits, otherwise pause → scroll left → loop."""

    def __init__(self, text: str, color: tuple, width: int):
        self.text = _ascii(text)
        self.width = width
        self.text_w = _bdf_width(self.text, _FONT)
        self.scrolls = self.text_w > width
        strip_w = self.text_w + (MARQUEE_GAP + self.text_w if self.scrolls else 0)
        self.strip = Image.new("RGB", (max(1, strip_w), _FONT_H), (0, 0, 0))
        _bdf_draw(self.strip, 0, 0, self.text, _FONT, color)
        if self.scrolls:
            _bdf_draw(self.strip, self.text_w + MARQUEE_GAP, 0, self.text, _FONT, color)
        self.offset = 0.0
        self.pause = MARQUEE_PAUSE_FRAMES

    def reset(self):
        self.offset = 0.0
        self.pause = MARQUEE_PAUSE_FRAMES

    def tick(self, speed: float):
        if not self.scrolls:
            return
        if self.pause > 0:
            self.pause -= 1
            return
        self.offset += speed
        if self.offset >= self.text_w + MARQUEE_GAP:
            self.reset()

    def paste_into(self, img: Image.Image, x: int, y: int):
        o = int(self.offset)
        img.paste(self.strip.crop((o, 0, o + self.width, _FONT_H)), (x, y))


def prepare_art(data: bytes):
    """Image bytes → 22×22 RGB, slightly boosted so it reads on the LED panel. None on failure."""
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGBA")                       # handle palette/transparency first
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
        im = im.resize((ART_SIZE, ART_SIZE), Image.LANCZOS)
        return ImageEnhance.Color(im).enhance(1.25)
    except Exception:
        return None


def _draw_note(img: Image.Image):
    """Placeholder when there's no artwork: a dim box with a music note."""
    px = img.load()
    for i in range(ART_SIZE):
        for (x, y) in ((ART_X + i, ART_Y), (ART_X + i, ART_Y + ART_SIZE - 1),
                       (ART_X, ART_Y + i), (ART_X + ART_SIZE - 1, ART_Y + i)):
            px[x, y] = (35, 35, 35)
    stem_x = ART_X + 13
    for y in range(ART_Y + 5, ART_Y + 15):
        px[stem_x, y] = COL_NOTE
    for k in range(4):                                # flag
        px[stem_x + 1 + k // 2, ART_Y + 5 + k] = COL_NOTE
    for dy in range(3):                               # note head
        for dx in range(4):
            if not (dy in (0, 2) and dx in (0, 3)):
                px[stem_x - 3 + dx, ART_Y + 14 + dy] = COL_NOTE


def render_frame(title: Marquee, artist: Marquee, album: Marquee, art, progress) -> Image.Image:
    """progress: 0..1, or None to omit the bar."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    title.paste_into(img, 0, TITLE_Y)
    if art is not None:
        img.paste(art, (ART_X, ART_Y))
    else:
        _draw_note(img)
    artist.paste_into(img, TEXT_X, ARTIST_Y)
    album.paste_into(img, TEXT_X, ALBUM_Y)
    if progress is not None:
        filled = int(round(max(0.0, min(1.0, progress)) * MATRIX_W))
        px = img.load()
        for x in range(MATRIX_W):
            px[x, BAR_Y] = COL_FILL if x < filled else COL_TRACK
    return img


def make_marquees(track: dict):
    return (Marquee(track.get("title", ""), COL_TITLE, MATRIX_W),
            Marquee(track.get("artist", ""), COL_ARTIST, TEXT_W),
            Marquee(track.get("album", ""), COL_ALBUM, TEXT_W))
