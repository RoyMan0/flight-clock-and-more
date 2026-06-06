"""
APNewsPlugin — vertical-scrolling breaking news ticker on the 64×32 matrix.

Layout:
  Rows 0–6:  White banner "BREAKING NEWS" (black text, 4x6.bdf)
  Row  7:    1px red separator
  Rows 8–31: Headlines scrolling bottom-to-top

Uses Google News RSS (news.google.com) which is publicly accessible and
includes AP wire stories. rsshub.app was blocked (403) for automated clients.
"""

import json
import logging
import os
import threading
import xml.etree.ElementTree as ET
from typing import Optional

import requests
from PIL import Image, ImageDraw

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

# Layout
MATRIX_W  = 64
MATRIX_H  = 32
HEADER_H  = 7      # rows 0-6: white banner (6px text + 1px top pad)
SEP_Y     = 7      # row 7: red separator
CONTENT_Y = 8      # rows 8-31: scrolling area
CONTENT_H = 24     # MATRIX_H - CONTENT_Y

FONT_H            = 6   # pixel height of a character cell
INNER_LINE_GAP    = 1   # px between wrapped lines of the same headline
HEADLINE_GAP      = 4   # px between separate headlines
MAX_TEXT_W        = 62  # leave 1px margin each side of the 64px strip
MIN_STRIP_H       = 256

# Colors
COLOR_BANNER_BG  = (255, 255, 255)
COLOR_BANNER_TXT = (0, 0, 0)
COLOR_SEP        = (220, 0, 0)
COLOR_TEXT       = (255, 255, 255)

_FEEDS = {
    "top_news":     "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "technology":   "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en",
    "world_news":   "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
    "politics":     "https://news.google.com/rss/headlines/section/topic/POLITICS?hl=en-US&gl=US&ceid=US:en",
    "sports":       "https://news.google.com/rss/headlines/section/topic/SPORTS?hl=en-US&gl=US&ceid=US:en",
    "entertainment":"https://news.google.com/rss/headlines/section/topic/ENTERTAINMENT?hl=en-US&gl=US&ceid=US:en",
    "business":     "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "science":      "https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en",
    "us_news":      "https://news.google.com/rss/headlines/section/geo/US?hl=en-US&gl=US&ceid=US:en",
}

_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CACHE_PATH = os.path.join(_BASE_DIR, "data", "ap_news_cache.json")
_FONTS_DIR  = os.path.join(_BASE_DIR, "fonts")

# ------------------------------------------------------------------
# BDF font renderer (copied from stock_ticker pattern)
# ------------------------------------------------------------------

def _parse_bdf(path: str) -> dict:
    glyphs: dict = {}
    cur: dict = {}
    rows: list = []
    reading = False
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ENCODING"):
                    cur = {"code": int(line.split()[1])}
                elif line.startswith("DWIDTH"):
                    cur["dw"] = int(line.split()[1])
                elif line.startswith("BBX"):
                    p = line.split()
                    cur.update(bbw=int(p[1]), bbh=int(p[2]),
                               xoff=int(p[3]), yoff=int(p[4]))
                elif line == "BITMAP":
                    reading, rows = True, []
                elif line == "ENDCHAR" and reading:
                    cur["rows"] = rows
                    glyphs[cur["code"]] = cur
                    reading = False
                elif reading:
                    rows.append(int(line, 16))
    except Exception as e:
        log.warning(f"[ap_news] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_width(text: str, font: dict) -> int:
    return sum(font[ord(c)]["dw"] for c in text if ord(c) in font)


def _bdf_draw(img: Image.Image, x: int, y: int, text: str,
              font: dict, color: tuple):
    if not font:
        return
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = font.get(ord(ch))
        if g is None:
            cx += 4
            continue
        bbw = g["bbw"]
        total_bits = ((bbw + 7) // 8) * 8
        for ri, rv in enumerate(g["rows"]):
            py = y + ri
            if py < 0 or py >= ih:
                continue
            for bi in range(bbw):
                if (rv >> (total_bits - 1 - bi)) & 1:
                    ox = cx + bi
                    if 0 <= ox < iw:
                        px[ox, py] = color
        cx += g["dw"]


_BDF = _parse_bdf(os.path.join(_FONTS_DIR, "4x6.bdf"))
_HEADER_TEXT = "BREAKING NEWS"


def _push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))


# ------------------------------------------------------------------
# RSS fetching and caching
# ------------------------------------------------------------------

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def _clean_title(title: str) -> str:
    """Strip the ' - Source Name' suffix Google News appends to every headline."""
    idx = title.rfind(" - ")
    if idx > 20:  # leave short titles intact (avoid stripping legitimate dashes)
        title = title[:idx]
    return title.strip()


def _fetch_headlines(enabled_feeds: dict) -> list:
    active = [k for k, v in enabled_feeds.items() if v]
    log.info(f"[ap_news] Fetching enabled feeds: {active}")
    if not active:
        return []
    headlines = []
    for key in active:
        url = _FEEDS.get(key)
        if not url:
            continue
        try:
            r = requests.get(url, headers=_HEADERS, timeout=(5, 15))
            r.raise_for_status()
            root = ET.fromstring(r.content)
            items = root.findall("channel/item")
            count = 0
            for item in items:
                t = _clean_title(item.findtext("title", "").strip())
                if t:
                    headlines.append(t)
                    count += 1
            log.info(f"[ap_news] {key}: {count} headlines")
        except Exception as e:
            log.warning(f"[ap_news] Feed '{key}' failed: {e}")
    return list(dict.fromkeys(headlines))


def _save_cache(headlines: list) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"headlines": headlines}, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception as e:
        log.warning(f"[ap_news] Cache write failed: {e}")


def _load_cache() -> list:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("headlines", [])
    except Exception:
        return []


# ------------------------------------------------------------------
# Rendering helpers
# ------------------------------------------------------------------

def _wrap_text(text: str, bdf: dict) -> list:
    """Word-wrap text into lines that each fit within MAX_TEXT_W pixels."""
    if _bdf_width(text, bdf) <= MAX_TEXT_W:
        return [text]
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip() if current else word
        if _bdf_width(candidate, bdf) <= MAX_TEXT_W:
            current = candidate
        else:
            if current:
                lines.append(current)
            # Single word wider than MAX_TEXT_W: hard-trim it
            if _bdf_width(word, bdf) > MAX_TEXT_W:
                while word and _bdf_width(word, bdf) > MAX_TEXT_W:
                    word = word[:-1]
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _headline_height(lines: list) -> int:
    """Pixel height consumed by a wrapped headline (text + inter-headline gap)."""
    n = len(lines)
    return n * FONT_H + (n - 1) * INNER_LINE_GAP + HEADLINE_GAP


def _build_vertical_strip(headlines: list, bdf: dict) -> Image.Image:
    if not headlines:
        headlines = ["No headlines"]

    wrapped = [_wrap_text(h, bdf) for h in headlines]

    single_pass_h = sum(_headline_height(lines) for lines in wrapped)

    reps = 1
    while single_pass_h * reps < MIN_STRIP_H:
        reps += 1
    strip_h = single_pass_h * reps

    img = Image.new("RGB", (MATRIX_W, strip_h), (0, 0, 0))

    y = 0
    for _ in range(reps):
        for lines in wrapped:
            for i, line in enumerate(lines):
                _bdf_draw(img, 1, y, line, bdf, COLOR_TEXT)
                # Advance: 1px inner gap between wrapped lines, 4px gap after last line
                y += FONT_H + (INNER_LINE_GAP if i < len(lines) - 1 else HEADLINE_GAP)

    return img


def _blit_vertical_strip(frame: Image.Image, strip: Image.Image, y_offset: int) -> None:
    sh = strip.height
    y_offset = y_offset % sh
    bottom = y_offset + CONTENT_H

    if bottom <= sh:
        frame.paste(strip.crop((0, y_offset, MATRIX_W, bottom)), (0, CONTENT_Y))
    else:
        part1 = strip.crop((0, y_offset, MATRIX_W, sh))
        part2 = strip.crop((0, 0, MATRIX_W, bottom - sh))
        frame.paste(part1, (0, CONTENT_Y))
        frame.paste(part2, (0, CONTENT_Y + sh - y_offset))


def _render_header(frame: Image.Image) -> None:
    draw = ImageDraw.Draw(frame)
    draw.rectangle([(0, 0), (MATRIX_W - 1, HEADER_H - 1)], fill=COLOR_BANNER_BG)
    w = _bdf_width(_HEADER_TEXT, _BDF)
    x = max(0, (MATRIX_W - w) // 2)
    _bdf_draw(frame, x, 1, _HEADER_TEXT, _BDF, COLOR_BANNER_TXT)
    draw.line([(0, SEP_Y), (MATRIX_W - 1, SEP_Y)], fill=COLOR_SEP)


# ------------------------------------------------------------------
# Plugin class
# ------------------------------------------------------------------

class APNewsPlugin(BasePlugin):
    PLUGIN_ID = "ap_news"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._headlines: list = []
        self._strip: Optional[Image.Image] = None
        self._y_offset: float = 0.0
        self._lock = threading.Lock()

    def reset(self):
        self._y_offset = 0.0

    def update(self):
        feeds = self.config.get("feeds", {})
        if not feeds:
            log.warning("[ap_news] No 'feeds' in config — defaulting to top_news")
            feeds = {"top_news": True}
        headlines = _fetch_headlines(feeds)
        if headlines:
            _save_cache(headlines)
        else:
            headlines = _load_cache()
            if not headlines:
                log.warning("[ap_news] No headlines fetched and no cache available")
        with self._lock:
            self._headlines = headlines
            self._strip = None
        log.debug(f"[ap_news] Updated: {len(headlines)} headlines")

    def draw(self) -> bool:
        with self._lock:
            headlines = list(self._headlines)
            if self._strip is None and headlines:
                self._strip = _build_vertical_strip(headlines, _BDF)
            strip = self._strip

        if strip is None:
            # Try loading from cache before giving up
            cached = _load_cache()
            if not cached:
                return False
            strip = _build_vertical_strip(cached, _BDF)
            with self._lock:
                if self._headlines == []:
                    self._headlines = cached
                self._strip = strip

        frame = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
        _render_header(frame)
        _blit_vertical_strip(frame, strip, int(self._y_offset))

        speed = float(self.config.get("scroll_speed", 1))
        self._y_offset = (self._y_offset + speed) % strip.height

        _push_pil_to_canvas(frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return False
