"""
StockTickerPlugin — dual-row scrolling stock ticker on the 64×32 matrix.

Layout:
  Top 16px row:    ETFs  (scrolls at etf_scroll_speed)
  Bottom 16px row: Stocks (scrolls at stock_scroll_speed)

Each ticker block (black background):
  y=0  (9px, 6x9.bdf):  SYMBOL in white
  y=9  (7px, 5x7.bdf):  $PRICE +/-X.X% in green (up) or red (down)
"""

import logging
import os
import threading
from typing import Optional

from PIL import Image

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

# Layout
MATRIX_W    = 64
MATRIX_H    = 32
ROW_H       = 16      # height of each ticker row
SYM_Y       = 0       # y-offset for symbol text within the row
PRICE_Y     = 9       # y-offset for price line (6x9 cell + 0px gap)
BLOCK_GAP   = 4       # black pixels between ticker blocks
MIN_STRIP_W = 256     # minimum strip width for smooth wrap

# Colours — black background, white symbol, green/red price
COLOR_SYM  = (255, 255, 255)
COLOR_UP   = (0, 220, 0)
COLOR_DOWN = (220, 0, 0)
COLOR_FLAT = (160, 160, 160)

# ------------------------------------------------------------------
# BDF pixel-font renderer
# ------------------------------------------------------------------

_FONTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "fonts")
)


def _parse_bdf(path: str) -> dict:
    """Parse BDF font file → {char_code: {dw, bbw, bbh, rows}}."""
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
        log.warning(f"[stock] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_width(text: str, font: dict) -> int:
    return sum(font[ord(c)]["dw"] for c in text if ord(c) in font)


def _bdf_draw(img: Image.Image, x: int, y: int, text: str,
              font: dict, color: tuple):
    """Stamp BDF glyphs into img. y is the top of the character cell."""
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


# 6x9: 9-px-tall cells for the symbol row
# 5x7: 7-px-tall cells for the price row  (9 + 7 = 16 = ROW_H)
_BDF_SYM   = _parse_bdf(os.path.join(_FONTS_DIR, "6x9.bdf"))
_BDF_PRICE = _parse_bdf(os.path.join(_FONTS_DIR, "5x7.bdf"))


class StockTickerPlugin(BasePlugin):
    PLUGIN_ID = "stock_ticker"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._etfs: list[dict] = []
        self._stocks: list[dict] = []
        self._etf_strip: Optional[Image.Image] = None
        self._stock_strip: Optional[Image.Image] = None
        self._etf_offset: float = 0.0
        self._stock_offset: float = 0.0
        self._data_lock = threading.Lock()

    # ------------------------------------------------------------------
    # BasePlugin
    # ------------------------------------------------------------------

    def reset(self):
        self._etf_offset = 0.0
        self._stock_offset = 0.0

    def update(self):
        """Fetch positions from Schwab API, falling back to Yahoo Finance for manual symbols if Schwab fails or is unconfigured."""
        app_key        = self.secrets.get("schwab_app_key", "")
        app_secret     = self.secrets.get("schwab_app_secret", "")
        token_path     = self.secrets.get("schwab_token_path", "")
        account_hashes = self.secrets.get("schwab_account_hashes", [])
        callback_url   = self.secrets.get(
            "schwab_callback_url", "https://flightsstockticker.duckdns.org/"
        )

        cfg_etfs   = self.config.get("etf_symbols", [])
        cfg_stocks = self.config.get("stock_symbols", [])

        etfs:   list = []
        stocks: list = []

        if app_key and app_secret and token_path and account_hashes:
            from plugins.stock_ticker.schwab_client import get_positions
            all_positions = get_positions(
                app_key, app_secret, token_path, account_hashes, callback_url
            )
            etfs   = [p for p in all_positions if p["asset_type"] == "ETF"]
            stocks = [p for p in all_positions if p["asset_type"] == "EQUITY"]

            if cfg_etfs:
                etfs = [p for p in etfs if p["symbol"] in cfg_etfs]
            if cfg_stocks:
                stocks = [p for p in stocks if p["symbol"] in cfg_stocks]

        # Fall back to Yahoo when Schwab returned nothing and manual symbols are configured
        # (covers both: Schwab not set up, and Schwab token expired/failed)
        if not etfs and not stocks and (cfg_etfs or cfg_stocks):
            from plugins.stock_ticker.yahoo_client import get_quotes
            all_symbols = list(cfg_etfs) + list(cfg_stocks)
            quotes = get_quotes(all_symbols)
            cfg_etf_set = set(s.upper() for s in cfg_etfs)
            etfs   = [q for q in quotes if q["symbol"] in cfg_etf_set]
            stocks = [q for q in quotes if q["symbol"] not in cfg_etf_set]
            log.debug(f"[stock] Yahoo Finance fallback: {len(etfs)} ETFs, {len(stocks)} stocks")

        with self._data_lock:
            self._etfs        = etfs
            self._stocks      = stocks
            self._etf_strip   = None   # force strip rebuild on next draw
            self._stock_strip = None

        log.debug(f"[stock] Updated: {len(etfs)} ETFs, {len(stocks)} stocks")

    def draw(self) -> bool:
        with self._data_lock:
            etfs   = list(self._etfs)
            stocks = list(self._stocks)

        if not etfs and not stocks:
            return False

        if self._etf_strip is None and etfs:
            self._etf_strip = _build_strip(etfs)
        if self._stock_strip is None and stocks:
            self._stock_strip = _build_strip(stocks)

        frame = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))

        etf_speed   = float(self.config.get("etf_scroll_speed", 2))
        stock_speed = float(self.config.get("stock_scroll_speed", 2))

        if self._etf_strip:
            _blit_strip(frame, self._etf_strip, int(self._etf_offset), y_dst=0)
            self._etf_offset = (self._etf_offset + etf_speed) % self._etf_strip.width

        if self._stock_strip:
            _blit_strip(frame, self._stock_strip, int(self._stock_offset), y_dst=ROW_H)
            self._stock_offset = (self._stock_offset + stock_speed) % self._stock_strip.width

        _push_pil_to_canvas(frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return False


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _price_str(entry: dict) -> str:
    pct   = entry["day_change_pct"]
    price = entry["price"]
    sign  = "+" if pct >= 0 else "-"
    # Drop cents for prices ≥$100 to keep the string compact
    price_part = f"${price:.0f}" if price >= 100 else f"${price:.2f}"
    return f"{price_part} {sign}{abs(pct):.1f}%"


def _block_width(entry: dict) -> int:
    sym_w   = _bdf_width(entry["symbol"], _BDF_SYM)
    price_w = _bdf_width(_price_str(entry), _BDF_PRICE)
    return max(sym_w, price_w) + BLOCK_GAP


def _build_strip(entries: list[dict]) -> Image.Image:
    """Render all ticker blocks into a wide horizontal strip (ROW_H px tall)."""
    total_w = sum(_block_width(e) for e in entries)
    strip_w = max(total_w, MIN_STRIP_W)

    full_entries: list = []
    while sum(_block_width(e) for e in full_entries) < strip_w:
        full_entries.extend(entries)

    img = Image.new("RGB", (strip_w, ROW_H), (0, 0, 0))

    x = 0
    for entry in full_entries:
        w = _block_width(entry)

        pct = entry["day_change_pct"]
        if pct > 0:
            price_color = COLOR_UP
        elif pct < 0:
            price_color = COLOR_DOWN
        else:
            price_color = COLOR_FLAT

        _bdf_draw(img, x, SYM_Y,   entry["symbol"],   _BDF_SYM,   COLOR_SYM)
        _bdf_draw(img, x, PRICE_Y, _price_str(entry), _BDF_PRICE, price_color)

        x += w
        if x >= strip_w:
            break

    return img


def _blit_strip(frame: Image.Image, strip: Image.Image, offset: int, y_dst: int):
    """Copy a 64-pixel horizontal window from strip into frame at y_dst."""
    sw     = strip.width
    offset = offset % sw
    right  = offset + MATRIX_W

    if right <= sw:
        frame.paste(strip.crop((offset, 0, right, ROW_H)), (0, y_dst))
    else:
        part1 = strip.crop((offset, 0, sw, ROW_H))
        part2 = strip.crop((0, 0, right - sw, ROW_H))
        frame.paste(part1, (0, y_dst))
        frame.paste(part2, (sw - offset, y_dst))


def _push_pil_to_canvas(img: Image.Image, display_manager):
    """Write PIL image pixels to the rgbmatrix canvas."""
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)

    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))
