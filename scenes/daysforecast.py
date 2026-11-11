import os
from utilities.paths import APP_ROOT   # bundled assets; the working folder is the data folder on the Mac
from datetime import datetime, timedelta
from PIL import Image

from utilities.animator import Animator
from setup import colours, fonts, frames, screen
from utilities.temperature import get_forecast_cached, get_period_forecast_cached
from config import NIGHT_START, NIGHT_END
from rgbmatrix import graphics

# Setup
DAY_COLOUR = colours.LIGHT_PINK
MIN_T_COLOUR = colours.LIGHT_MID_BLUE
MAX_T_COLOUR = colours.LIGHT_DARK_ORANGE
TEXT_FONT = fonts.extrasmall
FONT_HEIGHT = 5
DISTANCE_FROM_TOP = 32
ICON_SIZE = 10
FORECAST_SIZE = FONT_HEIGHT * 2 + ICON_SIZE
DAY_POSITION = DISTANCE_FROM_TOP - FONT_HEIGHT - ICON_SIZE
ICON_POSITION = DISTANCE_FROM_TOP - FONT_HEIGHT - ICON_SIZE
TEMP_POSITION = DISTANCE_FROM_TOP
NIGHT_START_TIME = datetime.strptime(NIGHT_START, "%H:%M")
NIGHT_END_TIME = datetime.strptime(NIGHT_END, "%H:%M")

def _hhmm(value, default):
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        return datetime.strptime(default, "%H:%M").time()


def forecast_choice(now=None) -> str:
    """What the bottom row shows right now: "days" (the next days) or "periods" (the next
    parts of today: Morn / Aft / Eve / Nite). clock_weather forecast_mode is "days" (default),
    "periods", or "scheduled": periods between forecast_periods_start and _end, days otherwise."""
    try:
        from core.config_manager import get_config
        cfg = get_config().get_plugin_config("clock_weather") or {}
    except Exception:
        return "days"
    mode = cfg.get("forecast_mode", "days")
    if mode == "periods":
        return "periods"
    if mode != "scheduled":
        return "days"
    t = (now or datetime.now()).time()
    a = _hhmm(cfg.get("forecast_periods_start"), "05:00")
    b = _hhmm(cfg.get("forecast_periods_end"), "10:00")
    inside = a <= t < b if a <= b else (t >= a or t < b)     # the window may wrap midnight
    return "periods" if inside else "days"


def current_forecast():
    """(choice, entries) for the bottom row. Day parts fall back to the daily forecast
    until the hourly one has arrived."""
    if forecast_choice() == "periods":
        parts = get_period_forecast_cached()
        if parts:
            return "periods", parts
    return "days", get_forecast_cached()


class DaysForecastScene(object):
    def __init__(self):
        super().__init__()
        self._redraw_forecast = True
        self._last_hour = None
        self._shown_choice = None
        self._retry_at = None          # day parts wanted but not fetched yet: look again then

    @Animator.KeyFrame.add(0)
    def _forecast_on_reset(self):
        """Fired by reset_scene() so forecast redraws after a canvas clear."""
        self._redraw_forecast = True

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def day(self, count):
        if getattr(self, '_iss_active', False):
            return
        # Ensure redraw when there's new scene selection or midnight brightness events
        now = datetime.now().replace(microsecond=0).time()
        if now == NIGHT_START_TIME.time() or now == NIGHT_END_TIME.time():
            self._redraw_forecast = True
            return

        # --- SCENE SWITCH HANDLING ---
        # If the parent system sets self._data when switching scenes:
        # redraw immediately but DO NOT trigger a fetch
        if len(self._data):
            self._redraw_forecast = True
            return

        current_hour = datetime.now().hour
        choice = forecast_choice()

        retry = self._retry_at is not None and datetime.now() >= self._retry_at
        if self._last_hour != current_hour or self._redraw_forecast or choice != self._shown_choice or retry:
            if self._last_hour is not None:
                self.draw_square(0, 12, 64, 32, colours.BLACK)

            self._last_hour = current_hour
            shown, forecast = current_forecast()

            if not forecast:
                return
            self._shown_choice = choice
            self._retry_at = datetime.now() + timedelta(seconds=30) if shown != choice else None

            self._redraw_forecast = False
            
            # --- RENDER FORECAST ---
            offset = 1
            space_width = screen.WIDTH // 3

            # Get current local date for the "Midnight Switch"
            now = datetime.now().astimezone()
            today_local = now.date()

            for day in forecast:
                raw_start = day["startTime"]
                # Parse the ISO timestamp including the timezone offset
                local_time = datetime.fromisoformat(raw_start)
                entry_date = local_time.date()

                # THE SWITCH LOGIC: 
                # If the entry date is before today, skip to next entry (days only;
                # day parts are already the upcoming ones)
                if "label" not in day and entry_date < today_local:
                    continue
                
                # Format the display data
                day_name = day.get("label") or local_time.strftime("%a")
                icon = day["values"]["weatherCodeFullDay"]
                if day.get("night") and os.path.exists(os.path.join(APP_ROOT, "icons", f"{icon}1.png")):
                    icon = f"{icon}1"                       # the night icon (moon) for Nite
                
                min_temp = f"{day['values']['temperatureMin']:.0f}"
                max_temp = f"{day['values']['temperatureMax']:.0f}"

                # --- Centering Calculations ---
                min_temp_width = len(min_temp) * 4
                max_temp_width = len(max_temp) * 4

                temp_x = offset + (space_width - min_temp_width - max_temp_width - 1) // 2 + 1
                max_temp_x = temp_x
                min_temp_x = temp_x + max_temp_width

                icon_x = offset + (space_width - ICON_SIZE) // 2
                day_x = offset + (space_width - len(day_name) * 4) // 2 + 1

                # --- Draw to Matrix ---
                graphics.DrawText(self.canvas, TEXT_FONT, day_x, DAY_POSITION, DAY_COLOUR, day_name)

                # Draw icon
                try:
                    image = Image.open(os.path.join(APP_ROOT, "icons", f"{icon}.png"))
                    try:
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:
                        resample = Image.ANTIALIAS
                    image.thumbnail((ICON_SIZE, ICON_SIZE), resample)
                    # Draw pixel-by-pixel (avoids Pillow/rgbmatrix unsafe_ptrs crash)
                    rgb = image.convert("RGB")
                    pixels = rgb.load()
                    w, h = rgb.size
                    for py in range(h):
                        for px in range(w):
                            r, g, b = pixels[px, py]
                            self.canvas.SetPixel(px + icon_x, py + ICON_POSITION, r, g, b)
                except FileNotFoundError:
                    # Fallback: draw a small colored square by weather category
                    wc = int(icon) if str(icon).isdigit() else 0
                    if wc == 1000:
                        fb = (255, 210, 0)    # clear — yellow
                    elif 1100 <= wc <= 1102:
                        fb = (200, 200, 80)   # partly cloudy — yellow-grey
                    elif wc == 1001:
                        fb = (140, 140, 140)  # cloudy — grey
                    elif 2000 <= wc <= 2100:
                        fb = (180, 180, 180)  # fog — light grey
                    elif 4000 <= wc <= 4201:
                        fb = (60, 120, 255)   # rain — blue
                    elif 5000 <= wc <= 5101:
                        fb = (180, 220, 255)  # snow — light blue
                    elif 6000 <= wc <= 6201:
                        fb = (100, 150, 255)  # freezing rain — blue-white
                    elif 7000 <= wc <= 7102:
                        fb = (160, 100, 220)  # ice pellets — purple
                    elif wc == 8000:
                        fb = (220, 140, 0)    # thunderstorm — amber
                    else:
                        fb = (100, 100, 100)  # unknown — grey
                    sq = 6
                    sx = icon_x + (ICON_SIZE - sq) // 2
                    sy = ICON_POSITION + (ICON_SIZE - sq) // 2
                    for py in range(sq):
                        for px in range(sq):
                            self.canvas.SetPixel(px + sx, py + sy, *fb)

                # Draw temperatures
                graphics.DrawText(self.canvas, TEXT_FONT, max_temp_x, TEMP_POSITION, MAX_T_COLOUR, max_temp)
                graphics.DrawText(self.canvas, TEXT_FONT, min_temp_x, TEMP_POSITION, MIN_T_COLOUR, min_temp)

                # Move to the next column
                offset += space_width
                
                # Safety: Stop drawing if we run out of screen space
                if offset >= screen.WIDTH:
                    break
