import logging
from datetime import datetime, timezone, timedelta
from utilities.animator import Animator
from setup import colours, fonts, frames
from rgbmatrix import graphics

logger = logging.getLogger(__name__)

# Set to True to force-activate the ISS scene for testing (no real pass needed)
_DEBUG_FORCE_PASS = False
_DEBUG_PASS = {
    "rise_time": None,       # filled at runtime
    "duration_sec": 300,
    "elapsed_sec": 30.0,
    "progress": 30 / 300,
    "remaining_sec": 270.0,
    "rise_azimuth": "NW",
    "set_azimuth": "SE",
    "max_elevation": 45,
}

_ISS_SOLAR = colours.LIGHT_ORANGE
_ISS_BODY  = colours.CYAN
_ISS_TRAIL = [colours.ORANGE, colours.ORANGE_DARK]


def _draw_iss_sprite(canvas, sx, sy):
    """Draw 7×3 ISS sprite: solar panel rows above/below, CYAN center pixel."""
    for dx in [0, 1, 2, 4, 5, 6]:
        c = _ISS_SOLAR
        canvas.SetPixel(sx + dx, sy - 1, c.red, c.green, c.blue)
        canvas.SetPixel(sx + dx, sy + 1, c.red, c.green, c.blue)
    for dx in [0, 1, 2]:
        c = _ISS_SOLAR
        canvas.SetPixel(sx + dx,     sy, c.red, c.green, c.blue)
        canvas.SetPixel(sx + 4 + dx, sy, c.red, c.green, c.blue)
    c = _ISS_BODY
    canvas.SetPixel(sx + 3, sy, c.red, c.green, c.blue)

    for i, tc in enumerate(_ISS_TRAIL, start=1):
        tx = sx - i
        if tx >= 0:
            canvas.SetPixel(tx, sy, tc.red, tc.green, tc.blue)


def _draw_progress_bar(canvas, progress):
    BAR_X, BAR_Y, BAR_W = 1, 23, 62
    cursor = BAR_X + int(progress * BAR_W)
    for x in range(BAR_X, BAR_X + BAR_W):
        if x < cursor:
            c = colours.LIMEGREEN
        elif x == cursor:
            c = colours.WHITE
            canvas.SetPixel(x, BAR_Y - 1, c.red, c.green, c.blue)
            canvas.SetPixel(x, BAR_Y + 1, c.red, c.green, c.blue)
        else:
            if x % 2 == 0:
                c = colours.LIGHT_BLUE
            else:
                continue
        canvas.SetPixel(x, BAR_Y, c.red, c.green, c.blue)


class ISSPassScene(object):
    def __init__(self):
        super().__init__()
        self._iss_active = False
        self._iss_blink = False

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def iss_pass(self, count):
        from core.config_manager import get_config as _get_config
        from utilities.iss import get_iss_pass_data

        alerts_cfg = _get_config().get_plugin_config("clock_weather").get("alerts", {})

        if not (alerts_cfg.get("iss_passes") and alerts_cfg.get("iss_fullscreen")):
            if self._iss_active:
                self._iss_active = False
                self._iss_clear_and_restore()
            return

        if _DEBUG_FORCE_PASS:
            data = dict(_DEBUG_PASS)
            data["rise_time"] = datetime.now(timezone.utc) - timedelta(seconds=30)
        else:
            try:
                data = get_iss_pass_data()
            except Exception as e:
                logger.debug(f"[ISS] Pass data error: {e}")
                data = None

        if data is None:
            if self._iss_active:
                self._iss_active = False
                self._iss_clear_and_restore()
            return

        if not self._iss_active:
            self.draw_square(0, 0, 64, 32, colours.BLACK)
        self._iss_active = True
        self._iss_blink = not self._iss_blink
        self._draw_iss_scene(data)

    def _iss_clear_and_restore(self):
        self.draw_square(0, 0, 64, 32, colours.BLACK)
        self._redraw_time = True
        self._redraw_temp = True
        self._redraw_forecast = True

    def _draw_iss_scene(self, data):
        # Clear dynamic regions
        self.draw_square(0, 9, 64, 14, colours.BLACK)
        self.draw_square(0, 22, 64, 25, colours.BLACK)
        self.draw_square(0, 26, 64, 32, colours.BLACK)

        # Title blink
        title_col = colours.WHITE if self._iss_blink else colours.GREY
        graphics.DrawText(self.canvas, fonts.regular, 1, 8, title_col, "ISS OVERHEAD")

        # Sprite + trail (center row y=11, sprite traverses x=1..56)
        sprite_x = 1 + int(data["progress"] * 55)
        _draw_iss_sprite(self.canvas, sprite_x, 11)

        # Direction/elevation if API provided azimuth data
        if data.get("rise_azimuth") and data.get("set_azimuth"):
            self.draw_square(0, 15, 64, 22, colours.BLACK)
            el = data.get("max_elevation")
            info = f"{data['rise_azimuth']}>{data['set_azimuth']}"
            if el is not None:
                info += f" {int(el)}\xb0"
            graphics.DrawText(self.canvas, fonts.extrasmall, 1, 21, colours.GREY, info)

        # Progress bar
        _draw_progress_bar(self.canvas, data["progress"])

        # Countdown centered at bottom
        rem = data["remaining_sec"]
        mins, secs = int(rem // 60), int(rem % 60)
        text = f"{mins}:{secs:02d}"
        x = max(1, (64 - len(text) * 5) // 2)
        graphics.DrawText(self.canvas, fonts.small, x, 31, colours.YELLOW, text)
