from datetime import datetime, timedelta
from PIL import Image

from utilities.animator import Animator
from setup import colours, fonts, frames, screen
from utilities.temperature import get_forecast_cached
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

class DaysForecastScene(object):
    def __init__(self):
        super().__init__()
        self._redraw_forecast = True
        self._last_hour = None

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

        if self._last_hour != current_hour or self._redraw_forecast:
            if self._last_hour is not None:
                self.draw_square(0, 12, 64, 32, colours.BLACK)

            self._last_hour = current_hour
            forecast = get_forecast_cached()

            if not forecast:
                return

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
                # If the entry date is before today, skip to next entry.
                if entry_date < today_local:
                    continue
                
                # Format the display data
                day_name = local_time.strftime("%a")
                icon = day["values"]["weatherCodeFullDay"]
                
                min_temp = f"{day['values']['temperatureMin']:.0f}"
                max_temp = f"{day['values']['temperatureMax']:.0f}"

                # --- Centering Calculations ---
                min_temp_width = len(min_temp) * 4
                max_temp_width = len(max_temp) * 4

                temp_x = offset + (space_width - min_temp_width - max_temp_width - 1) // 2 + 1
                max_temp_x = temp_x
                min_temp_x = temp_x + max_temp_width

                icon_x = offset + (space_width - ICON_SIZE) // 2
                day_x = offset + (space_width - 12) // 2 + 1

                # --- Draw to Matrix ---
                graphics.DrawText(self.canvas, TEXT_FONT, day_x, DAY_POSITION, DAY_COLOUR, day_name)

                # Draw icon
                try:
                    image = Image.open(f"icons/{icon}.png")
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
