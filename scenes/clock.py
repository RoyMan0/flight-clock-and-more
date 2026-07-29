from datetime import datetime, timezone, timedelta
from utilities.temperature import grab_forecast
from utilities.animator import Animator
from setup import colours, fonts, frames
from rgbmatrix import graphics
import logging
from config import CLOCK_FORMAT, NIGHT_END, NIGHT_START

# Configure logging
#logging.basicConfig(filename='myapp.log', level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Setup
CLOCK_FONT = fonts.large_bold
CLOCK_FONT_SMALL = fonts.small
CLOCK_POSITION = (0, 11)
CLOCK_POSITION_SMALL = (0, 6)
DAY_COLOUR = colours.LIGHT_ORANGE
NIGHT_COLOUR = colours.LIGHT_BLUE


# Convert NIGHT_START and NIGHT_END to datetime objects 
NIGHT_START_TIME = datetime.strptime(NIGHT_START, "%H:%M") 
NIGHT_END_TIME = datetime.strptime(NIGHT_END, "%H:%M")

class ClockScene(object):
    def __init__(self):
        super().__init__()
        self._last_time = None
        self._last_clock_font = CLOCK_FONT
        self._last_clock_pos = CLOCK_POSITION
        self.today_sunrise = None
        self.today_sunset = None
        self.last_fetch_date = None  # Store the date of the last forecast fetch
        self._forecast_retry_after = 0  # Epoch: don't retry before this

    def calculate_sunrise_sunset(self):
        now = datetime.now()

        try:
            # Only fetch forecast if it's a new day or if no cached data
            if self.last_fetch_date != now.date():
                # Clear stale values immediately — yesterday's times must not linger
                self.today_sunrise = None
                self.today_sunset = None

                # Cooldown: don't hammer the API on repeated failures
                if datetime.now(timezone.utc).timestamp() < self._forecast_retry_after:
                    return None, None

                forecast = grab_forecast(tag="ClockScene")
                if not forecast:  # None or empty list
                    logging.error("Forecast data missing or API error.")
                    self._forecast_retry_after = datetime.now(timezone.utc).timestamp() + 300  # 5 min
                    return None, None

                today_str = now.strftime('%Y-%m-%d')
                for day in forecast:
                    forecast_date = day['startTime'][:10]
                    if forecast_date == today_str:
                        # Parse as UTC (Z = UTC per spec)
                        utc_sunrise = datetime.strptime(day['values']['sunriseTime'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
                        utc_sunset  = datetime.strptime(day['values']['sunsetTime'],  '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

                        # Sanity check: Tomorrow.io sometimes returns local time with
                        # a Z suffix instead of true UTC. If the local sunrise hour is
                        # outside 4am–10am, the value is local-as-UTC and needs fixing.
                        local_tz = datetime.now().astimezone().tzinfo
                        local_rise_h = utc_sunrise.astimezone(local_tz).hour
                        if not (4 <= local_rise_h <= 10):
                            logging.info(f"[ClockScene] sunriseTime looks like local time (local hour={local_rise_h}) — reinterpreting")
                            utc_sunrise = utc_sunrise.replace(tzinfo=None).replace(tzinfo=local_tz).astimezone(timezone.utc)
                            utc_sunset  = utc_sunset.replace(tzinfo=None).replace(tzinfo=local_tz).astimezone(timezone.utc)

                        self.today_sunrise = utc_sunrise
                        self.today_sunset  = utc_sunset
                        self.last_fetch_date = now.date()
                        local_rise = utc_sunrise.astimezone(local_tz).strftime('%H:%M')
                        local_set  = utc_sunset.astimezone(local_tz).strftime('%H:%M')
                        logging.info(f"[ClockScene] sunrise={local_rise} sunset={local_set} local for {today_str}")
                        break

                if self.today_sunrise is None:
                    logging.warning(f"[ClockScene] No matching day found in forecast for {today_str} (got {[d['startTime'][:10] for d in forecast]})")

        except Exception as e:
            logging.error(f"Error fetching forecast: {e}")
            return None, None

        return self.today_sunrise, self.today_sunset

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def clock(self, count):
        if getattr(self, '_iss_active', False):
            return
        if len(self._data):
            self._redraw_time = True
            return

        now = datetime.now()
        clock_format = "%l:%M" if CLOCK_FORMAT == "12hr" else "%H:%M"
        current_time = now.strftime(clock_format)

        utc_sunrise, utc_sunset = self.calculate_sunrise_sunset()
        now_utc = datetime.now(timezone.utc)

        if utc_sunrise is None or utc_sunset is None:
            clock_color = colours.RED
        elif utc_sunrise <= now_utc < utc_sunset:
            clock_color = DAY_COLOUR
        else:
            clock_color = NIGHT_COLOUR

        has_alerts = bool(getattr(self, "_has_active_alerts", False))
        font = CLOCK_FONT_SMALL if has_alerts else CLOCK_FONT
        pos = CLOCK_POSITION_SMALL if has_alerts else CLOCK_POSITION

        if self._last_time and (self._last_time != current_time or getattr(self, "_redraw_time", False)):
            graphics.DrawText(
                self.canvas,
                self._last_clock_font,
                self._last_clock_pos[0],
                self._last_clock_pos[1],
                colours.BLACK,
                self._last_time,
            )

        self._last_time = current_time
        self._last_clock_font = font
        self._last_clock_pos = pos

        graphics.DrawText(
            self.canvas,
            font,
            pos[0],
            pos[1],
            clock_color,
            current_time,
        )

        self._redraw_time = False