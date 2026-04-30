"""
StockTickerPlugin — dual-row scrolling stock ticker on the 64×32 matrix.

Layout:
  Top 16px row:    ETFs (scrolls at etf_scroll_speed)
  Bottom 16px row: Individual stocks (scrolls at stock_scroll_speed)

Each ticker block (width varies by text):
  Sub-row 0 (y+0..5):  SYMBOL   in white
  Sub-row 1 (y+8..13): $PRICE ▲/▼ +X.XX%   in green (up) or red (down)
  Background rectangle: dark green (up) or dark red (down)

The entire strip for each row is rendered into a wide PIL image (off-screen),
then a 64-pixel window is blitted to the canvas based on the current scroll offset.
The two rows scroll independently at their configured speeds.
"""

import logging
import time
import threading
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

# Layout constants
MATRIX_W = 64
MATRIX_H = 32
ROW_H = 16          # Height of each ticker row (ETF row + stock row)
BLOCK_PAD = 4       # Horizontal pixels between ticker blocks
MIN_STRIP_W = 256   # Minimum off-screen strip width (wraps when scrolled past)

# Colors
COLOR_BG_UP   = (0, 40, 0)
COLOR_BG_DOWN = (50, 0, 0)
COLOR_SYM     = (255, 255, 255)
COLOR_UP      = (0, 220, 0)
COLOR_DOWN    = (220, 0, 0)
COLOR_FLAT    = (180, 180, 180)

# Try to load a small bitmap font; fall back to PIL default
def _load_font(size: int = 6):
    try:
        # Use a tiny truetype font if available on the Pi
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except Exception:
        return ImageFont.load_default()

FONT_SYM  = _load_font(6)
FONT_PRICE = _load_font(6)


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
        self._last_update: float = 0.0
        self._cycle_done = False

    # ------------------------------------------------------------------
    # BasePlugin
    # ------------------------------------------------------------------

    def reset(self):
        self._etf_offset = 0.0
        self._stock_offset = 0.0
        self._cycle_done = False

    def update(self):
        """Fetch positions from Schwab API."""
        app_key = self.secrets.get("schwab_app_key", "")
        app_secret = self.secrets.get("schwab_app_secret", "")
        token_path = self.secrets.get("schwab_token_path", "")
        account_hash = self.secrets.get("schwab_account_hash", "")

        # Also support manual override lists from config
        cfg_etfs = self.config.get("etf_symbols", [])
        cfg_stocks = self.config.get("stock_symbols", [])

        if app_key and app_secret and token_path and account_hash:
            from plugins.stock_ticker.schwab_client import get_positions
            all_positions = get_positions(app_key, app_secret, token_path, account_hash)

            etfs = [p for p in all_positions if p["asset_type"] == "ETF"]
            stocks = [p for p in all_positions if p["asset_type"] == "EQUITY"]

            # If user has manual override lists, filter to those symbols
            if cfg_etfs:
                etfs = [p for p in etfs if p["symbol"] in cfg_etfs]
            if cfg_stocks:
                stocks = [p for p in stocks if p["symbol"] in cfg_stocks]
        else:
            # No Schwab credentials — show stub data for configured symbols
            etfs = [_stub(s) for s in cfg_etfs]
            stocks = [_stub(s) for s in cfg_stocks]

        with self._data_lock:
            self._etfs = etfs
            self._stocks = stocks
            self._etf_strip = None
            self._stock_strip = None
        log.debug(f"[stock] Updated: {len(etfs)} ETFs, {len(stocks)} stocks")

    def draw(self) -> bool:
        with self._data_lock:
            etfs = list(self._etfs)
            stocks = list(self._stocks)

        if not etfs and not stocks:
            return False

        # Build strips lazily (rebuild when data changes)
        if self._etf_strip is None and etfs:
            self._etf_strip = _build_strip(etfs)
        if self._stock_strip is None and stocks:
            self._stock_strip = _build_strip(stocks)

        # Compose frame
        frame = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))

        etf_speed = float(self.config.get("etf_scroll_speed", 2))
        stock_speed = float(self.config.get("stock_scroll_speed", 5))

        if self._etf_strip:
            _blit_strip(frame, self._etf_strip, int(self._etf_offset), y_dst=0)
            self._etf_offset = (self._etf_offset + etf_speed) % self._etf_strip.width

        if self._stock_strip:
            _blit_strip(frame, self._stock_strip, int(self._stock_offset), y_dst=ROW_H)
            self._stock_offset = (self._stock_offset + stock_speed) % self._stock_strip.width

        _push_pil_to_canvas(frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return False  # Scrolls continuously for display_duration


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _stub(symbol: str) -> dict:
    """Placeholder entry when no API credentials are configured."""
    return {
        "symbol": symbol,
        "asset_type": "EQUITY",
        "price": 0.0,
        "day_change": 0.0,
        "day_change_pct": 0.0,
        "market_value": 0.0,
    }


def _block_width(entry: dict) -> int:
    """Estimate pixel width of one ticker block."""
    sym_len = len(entry["symbol"]) * 6
    price_str = _price_str(entry)
    price_len = len(price_str) * 5
    return max(sym_len, price_len) + BLOCK_PAD * 2


def _price_str(entry: dict) -> str:
    arrow = "▲" if entry["day_change_pct"] >= 0 else "▼"
    return f"${entry['price']:.2f}{arrow}{abs(entry['day_change_pct']):.1f}%"


def _build_strip(entries: list[dict]) -> Image.Image:
    """Render all ticker blocks into a wide horizontal PIL image (ROW_H pixels tall)."""
    total_w = sum(_block_width(e) for e in entries)
    # Make strip at least 2× screen width so the seam wraps invisibly
    strip_w = max(total_w, MIN_STRIP_W)
    # Repeat entries to fill width
    full_entries = []
    while sum(_block_width(e) for e in full_entries) < strip_w:
        full_entries.extend(entries)

    img = Image.new("RGB", (strip_w, ROW_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = 0
    for entry in full_entries:
        w = _block_width(entry)
        is_up = entry["day_change_pct"] >= 0
        bg = COLOR_BG_UP if is_up else COLOR_BG_DOWN
        fg = COLOR_UP if is_up else COLOR_DOWN

        # Background
        draw.rectangle([x, 0, x + w - 1, ROW_H - 1], fill=bg)

        # Symbol (top sub-row)
        sym = entry["symbol"]
        draw.text((x + BLOCK_PAD, 1), sym, font=FONT_SYM, fill=COLOR_SYM)

        # Price + arrow + % (bottom sub-row)
        price_str = _price_str(entry)
        draw.text((x + BLOCK_PAD, ROW_H // 2 + 1), price_str, font=FONT_PRICE, fill=fg)

        x += w
        if x >= strip_w:
            break

    return img


def _blit_strip(frame: Image.Image, strip: Image.Image, offset: int, y_dst: int):
    """Copy a 64-pixel horizontal window from strip into frame at y_dst."""
    sw = strip.width
    offset = offset % sw

    # Handle wrap-around
    right = offset + MATRIX_W
    if right <= sw:
        region = strip.crop((offset, 0, right, ROW_H))
        frame.paste(region, (0, y_dst))
    else:
        part1 = strip.crop((offset, 0, sw, ROW_H))
        part2 = strip.crop((0, 0, right - sw, ROW_H))
        frame.paste(part1, (0, y_dst))
        frame.paste(part2, (sw - offset, y_dst))


def _push_pil_to_canvas(img: Image.Image, display_manager):
    """Write PIL image pixels to the rgbmatrix canvas (or software canvas)."""
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)

    # Keep PIL mirror updated for web dashboard snapshot
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))
