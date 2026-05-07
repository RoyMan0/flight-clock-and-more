"""
SnowReportPlugin — shows snow conditions for configured ski resorts.

Data source: OnTheSnow API (no key required).
Cycles through each resort for seconds_per_resort, then signals cycle complete.

Display layout (64×32):
  Row 0–10:  Resort name (left) + ❄ Base depth (right)
  Row 11–21: New snow 24h + conditions (scrolling if needed)
  Row 22–31: Open trails / total trails
"""

import logging
import time
import threading
from datetime import datetime
import requests
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32

OTS_API = "https://skiapi.onthesnow.com/api/resort/{resort_id}"
OTS_HEADERS = {"User-Agent": "LEDMatrix/1.0"}

# Colors
COL_NAME  = (255, 255, 255)
COL_SNOW  = (100, 180, 255)
COL_NEW   = (0, 220, 100)
COL_COND  = (200, 200, 200)
COL_TRAIL = (255, 200, 0)
COL_ERR   = (200, 0, 0)

def _load_font(size: int = 6):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except Exception:
        return ImageFont.load_default()

FONT_LG = _load_font(8)
FONT_SM = _load_font(6)


class SnowReportPlugin(BasePlugin):
    PLUGIN_ID = "snow_report"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._reports: list[dict] = []
        self._current_idx: int = 0
        self._idx_start: float = 0.0
        self._lock = threading.Lock()
        self._cycle_done = False

    # ------------------------------------------------------------------
    # BasePlugin
    # ------------------------------------------------------------------

    def reset(self):
        self._current_idx = 0
        self._idx_start = time.monotonic()
        self._cycle_done = False

    def update(self):
        resorts = self.config.get("resorts", [])
        reports = []
        for resort in resorts:
            report = _fetch_resort(resort.get("id"), resort.get("name", "?"))
            reports.append(report)
        with self._lock:
            self._reports = reports
        log.debug(f"[snow] Updated {len(reports)} resorts")

    def draw(self) -> bool:
        if self.config.get("morning_only", False):
            hour = datetime.now().hour
            if not (6 <= hour < 10):
                return False

        with self._lock:
            reports = list(self._reports)

        if not reports:
            return False

        spr = float(self.config.get("seconds_per_resort", 8))
        now = time.monotonic()

        # Advance to next resort when timer expires
        if now - self._idx_start >= spr:
            self._current_idx += 1
            self._idx_start = now
            if self._current_idx >= len(reports):
                self._cycle_done = True
                self._current_idx = 0  # wrap for next cycle

        report = reports[min(self._current_idx, len(reports) - 1)]
        frame = _render_report(report)
        _push_pil_to_canvas(frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return self._cycle_done


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------

def _fetch_resort(resort_id: Optional[int], name: str) -> dict:
    base = {
        "name": name,
        "base_depth": None,
        "new_snow_24h": None,
        "conditions": "N/A",
        "open_trails": None,
        "total_trails": None,
        "error": None,
    }
    if resort_id is None:
        base["error"] = "No ID"
        return base
    try:
        r = requests.get(
            OTS_API.format(resort_id=resort_id),
            headers=OTS_HEADERS,
            timeout=10,
        )
        if r.status_code != 200:
            base["error"] = f"HTTP {r.status_code}"
            return base
        data = r.json()
        base.update({
            "base_depth": data.get("snowBaseIn") or data.get("snowBaseCm"),
            "new_snow_24h": data.get("newSnow24In") or data.get("newSnow24Cm"),
            "conditions": data.get("surfaceConditions") or data.get("surfaceCondition") or "N/A",
            "open_trails": data.get("openTrails"),
            "total_trails": data.get("totalTrails"),
        })
    except Exception as e:
        base["error"] = str(e)[:20]
    return base


# ------------------------------------------------------------------
# Rendering
# ------------------------------------------------------------------

def _render_report(report: dict) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    name = report["name"][:10]  # truncate long names

    if report.get("error"):
        draw.text((1, 1), name, font=FONT_SM, fill=COL_NAME)
        draw.text((1, 12), report["error"], font=FONT_SM, fill=COL_ERR)
        return img

    # Row 1: Name + base depth
    draw.text((1, 1), name.upper(), font=FONT_SM, fill=COL_NAME)
    if report["base_depth"] is not None:
        base_str = f'❄{int(report["base_depth"])}"'
        draw.text((MATRIX_W - len(base_str) * 5 - 1, 1), base_str, font=FONT_SM, fill=COL_SNOW)

    # Row 2: New snow + conditions
    new_snow = f'+{int(report["new_snow_24h"])}"' if report["new_snow_24h"] else "  0"
    cond = (report["conditions"] or "")[:10]
    draw.text((1, 12), new_snow, font=FONT_SM, fill=COL_NEW)
    draw.text((20, 12), cond, font=FONT_SM, fill=COL_COND)

    # Row 3: Trails open / total
    if report["open_trails"] is not None and report["total_trails"] is not None:
        trail_str = f'{report["open_trails"]}/{report["total_trails"]} trails'
        draw.text((1, 23), trail_str, font=FONT_SM, fill=COL_TRAIL)

    return img


def _push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))
