"""
APNewsPlugin — vertical-scrolling AP breaking news ticker on the 64×32 matrix.

Layout:
  Rows 0–6:  White banner "AP BREAKING NEWS" (black text, 4x6.bdf)
  Row  7:    1px red separator
  Rows 8–31: Headlines scrolling bottom-to-top
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

LINE_H       = 7   # 6px text + 1px gap
MIN_STRIP_H  = 256

# Colors
COLOR_BANNER_BG  = (255, 255, 255)
COLOR_BANNER_TXT = (0, 0, 0)
COLOR_SEP        = (220, 0, 0)
COLOR_TEXT       = (255, 255, 255)

_FEEDS = {
    "top_news":     "https://rsshub.app/apnews/topics/apf-topnews",
    "technology":   "https://rsshub.app/apnews/topics/technology",
    "world_news":   "https://rsshub.app/apnews/topics/world-news",
    "politics":     "https://rsshub.app/apnews/topics/politics",
    "sports":       "https://rsshub.app/apnews/topics/sports",
    "entertainment":"https://rsshub.app/apnews/topics/entertainment",
    "business":     "https://rsshub.app/apnews/topics/business",
    "science":      "https://rsshub.app/apnews/topics/science",
    "us_news":      "https://rsshub.app/apnews/topics/us-news",
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
_HEADER_TEXT = "AP BREAKING NEWS"


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

def _fetch_headlines(enabled_feeds: dict) -> list:
    headlines = []
    for key, url in _FEEDS.items():
        if not enabled_feeds.get(key, False):
            continue
        try:
            r = requests.get(url, timeout=(5, 15))
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall("channel/item"):
                t = item.findtext("title", "").strip()
                if t:
                    headlines.append(t)
            log.debug(f"[ap_news] Fetched {key}: {len(root.findall('channel/item'))} items")
        except Exception as e:
            log.warning(f"[ap_news] Feed '{key}' failed: {e}")
    # Deduplicate preserving order
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

def _truncate(text: str, bdf: dict, max_w: int = 62) -> str:
    if _bdf_width(text, bdf) <= max_w:
        return text
    ellipsis = "…"
    while text and _bdf_width(text + ellipsis, bdf) > max_w:
        text = text[:-1]
    return text + ellipsis


def _build_vertical_strip(headlines: list, bdf: dict) -> Image.Image:
    # Ensure there's at least one headline
    if not headlines:
        headlines = ["No headlines"]

    # Truncate each headline to fit 62px (leave 1px margin each side)
    truncated = [_truncate(h, bdf) for h in headlines]

    # Two blank lines of padding between last and first entry (seamless wrap)
    single_pass_h = len(truncated) * LINE_H + LINE_H * 2

    reps = 1
    while single_pass_h * reps < MIN_STRIP_H:
        reps += 1
    strip_h = single_pass_h * reps

    img = Image.new("RGB", (MATRIX_W, strip_h), (0, 0, 0))

    y = 0
    for _ in range(reps):
        for text in truncated:
            _bdf_draw(img, 1, y, text, bdf, COLOR_TEXT)
            y += LINE_H
        y += LINE_H * 2  # blank padding between passes

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
