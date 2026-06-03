"""
SetupQRPlugin — draws the WiFi setup QR code on the 64×32 LED matrix.

Layout (25×25 QR + right panel):
  cols 0–24  : QR code (25×25 at 1px/module, border=2, centered vertically)
  cols 26–63 : text panel
    row  1   : "Join Wi-Fi"
    row  9   : SSID value
    row 17   : password value
    row 25   : "→ 10.42.0.1"

Falls back to scrolling text if the qrcode library is unavailable or the
content doesn't fit in a version-1 code.
"""

import logging
import os
import time

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

_FONTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "fonts"))
_TINY_FONT  = os.path.join(_FONTS_DIR, "tom-thumb.bdf")    # 4×6 — narrowest
_SMALL_FONT = os.path.join(_FONTS_DIR, "4x6.bdf")          # 4×6 — next up

# Colors
_WHITE  = (255, 255, 255)
_YELLOW = (254, 167, 39)
_CYAN   = (40,  200, 230)
_GREY   = (160, 160, 160)
_BLACK  = (0,   0,   0)

# QR placement
_QR_X    = 0
_QR_Y    = 3   # top row of QR block (25 rows → 3..27, centred in 32)
_QR_SIZE = 25  # matches version=1, border=2 → 21+2+2 = 25

# Text panel start column
_TEXT_X  = 27


class SetupQRPlugin(BasePlugin):
    PLUGIN_ID = "setup_qr"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)

        from core.setup_mode import HOTSPOT_SSID, HOTSPOT_PASSWORD, HOTSPOT_IP, generate_wifi_qr_matrix
        self._ssid     = HOTSPOT_SSID
        self._password = HOTSPOT_PASSWORD
        self._ip       = HOTSPOT_IP

        self._qr_matrix: list[list[bool]] | None = generate_wifi_qr_matrix(
            self._ssid, self._password
        )

        # Scroll state for fallback text mode
        self._scroll_x   = _TEXT_X
        self._last_scroll = time.monotonic()

        # Try to load graphics helpers (not available in pure software mode
        # without the rgbmatrix C extension, but they degrade gracefully)
        self._graphics = None
        self._font_tiny  = None
        self._font_small = None
        try:
            from rgbmatrix import graphics
            self._graphics = graphics
            self._font_tiny = graphics.Font()
            self._font_tiny.LoadFont(_TINY_FONT)
            self._font_small = graphics.Font()
            self._font_small.LoadFont(_SMALL_FONT)
        except Exception:
            pass  # software mode — text drawing skipped

    # ------------------------------------------------------------------

    def draw(self) -> bool:
        canvas = self.display_manager.canvas
        canvas.Clear()

        if self._qr_matrix:
            self._draw_qr(canvas)
        self._draw_text(canvas)
        return True

    def update(self):
        pass

    def has_content(self) -> bool:
        return True

    def is_cycle_complete(self) -> bool:
        return False  # show indefinitely

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _draw_qr(self, canvas):
        """Write QR pixels directly via SetPixel."""
        matrix = self._qr_matrix
        rows = len(matrix)
        cols = len(matrix[0]) if rows else 0
        for row_idx in range(rows):
            for col_idx in range(cols):
                x = _QR_X + col_idx
                y = _QR_Y + row_idx
                if x >= 64 or y >= 32:
                    continue
                if matrix[row_idx][col_idx]:
                    canvas.SetPixel(x, y, *_WHITE)
                else:
                    canvas.SetPixel(x, y, *_BLACK)

    def _draw_text(self, canvas):
        if self._graphics is None:
            return

        g = self._graphics
        tiny  = self._font_tiny
        small = self._font_small

        def _color(rgb):
            return g.Color(*rgb)

        # "Join Wi-Fi" header
        g.DrawText(canvas, tiny, _TEXT_X, 6, _color(_YELLOW), "Join Wi-Fi")

        # Divider line
        for x in range(_TEXT_X, 64):
            canvas.SetPixel(x, 8, *_GREY)

        # SSID label + value
        g.DrawText(canvas, tiny, _TEXT_X, 14, _color(_GREY), "SSID:")
        g.DrawText(canvas, tiny, _TEXT_X, 20, _color(_CYAN), self._ssid)

        # Password label + value
        g.DrawText(canvas, tiny, _TEXT_X, 26, _color(_GREY), "PW:")
        g.DrawText(canvas, tiny, _TEXT_X, 32, _color(_CYAN), self._password)
