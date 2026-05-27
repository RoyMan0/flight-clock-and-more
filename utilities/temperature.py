from datetime import datetime, timedelta
import time
import logging
import socket
import json
import os
from typing import Optional

from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry

_CACHE_DIR = ".cache"
_TEMP_CACHE_FILE = os.path.join(_CACHE_DIR, "temperature.json")
_FORECAST_CACHE_FILE = os.path.join(_CACHE_DIR, "forecast.json")
_FILE_CACHE_TTL = 7200  # 2-hour file cache TTL (fallback on reboot/API failure)


def _write_file_cache(path: str, data) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning(f"Could not write cache file {path}: {e}")


def _read_file_cache(path: str):
    try:
        if time.time() - os.path.getmtime(path) > _FILE_CACHE_TTL:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# After a 429 response, block all API calls for this many minutes
_RATE_LIMIT_BACKOFF_MINUTES = 10
_rate_limited_until: Optional[datetime] = None
_rate_limit_logged_until: Optional[datetime] = None  # suppress repeated warnings

# Module-level cache — survives plugin mode switches for the process lifetime
_TEMP_TTL = 300       # 5 min between successful fetches
_FORECAST_TTL = 3600  # 1 hr between successful fetches

_temp_data: Optional[tuple] = None
_temp_next_fetch: Optional[datetime] = None

_forecast_data: Optional[list] = None
_forecast_next_fetch: Optional[datetime] = None

# Attempt to load config data
try:
    from config import TOMORROW_API_KEY
    from config import FORECAST_DAYS

except (ModuleNotFoundError, NameError, ImportError):
    # If there's no config data
    TOMORROW_API_KEY = None
    FORECAST_DAYS = 3

_last_units: Optional[str] = None


def _get_units() -> str:
    try:
        from core.config_manager import get_config as _get_config
        u = _get_config().get("location", "units", default="imperial")
        return u if u in ("metric", "imperial") else "imperial"
    except Exception:
        return "imperial"


def _flush_if_units_changed() -> None:
    global _last_units, _temp_data, _temp_next_fetch, _forecast_data, _forecast_next_fetch
    current = _get_units()
    if _last_units is not None and _last_units != current:
        _temp_data = None
        _temp_next_fetch = None
        _forecast_data = None
        _forecast_next_fetch = None
        try:
            os.remove(_TEMP_CACHE_FILE)
        except OSError:
            pass
        try:
            os.remove(_FORECAST_CACHE_FILE)
        except OSError:
            pass
        logging.info(f"[Temp] Units changed {_last_units}→{current}, cache flushed")
    _last_units = current

from config import TEMPERATURE_LOCATION

def is_dns_error(exc: Exception) -> bool:
    cause = exc
    while cause:
        if isinstance(cause, socket.gaierror):
            return True
        cause = cause.__cause__
    return False
    
_session = None

def get_session() -> Session:
    global _session
    if _session is None:
        _session = Session()

        retries = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=2,
            allowed_methods=["GET", "POST"],
            # 429 intentionally excluded — we handle rate limits ourselves with backoff
            status_forcelist=[500, 502, 503, 504],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=2,
            pool_maxsize=2,
        )

        _session.mount("https://", adapter)
        _session.mount("http://", adapter)

    return _session
    
# Weather API
TOMORROW_API_URL = "https://api.tomorrow.io/v4"

def _is_rate_limited() -> bool:
    global _rate_limited_until, _rate_limit_logged_until
    if _rate_limited_until and datetime.now() < _rate_limited_until:
        # Log at most once per minute to avoid flooding the log
        if _rate_limit_logged_until is None or datetime.now() >= _rate_limit_logged_until:
            remaining = int((_rate_limited_until - datetime.now()).total_seconds() // 60)
            logging.warning(f"Tomorrow.io rate-limited; skipping requests for {remaining}m more")
            _rate_limit_logged_until = datetime.now() + timedelta(minutes=1)
        return True
    return False


def _set_rate_limited():
    global _rate_limited_until
    until = datetime.now() + timedelta(minutes=_RATE_LIMIT_BACKOFF_MINUTES)
    _rate_limited_until = until
    logging.error(f"Tomorrow.io 429 — backing off until {until.strftime('%H:%M')}")


def grab_temperature_and_humidity():
    _flush_if_units_changed()
    if _is_rate_limited():
        return None, None

    try:
        s = get_session()
        request = s.get(
            f"{TOMORROW_API_URL}/weather/realtime",
            params={
                "location": TEMPERATURE_LOCATION,
                "units": _get_units(),
                "apikey": TOMORROW_API_KEY
            },
            timeout=(5, 20)
        )

        if request.status_code == 429:
            _set_rate_limited()
            return None, None

        request.raise_for_status()

        data = request.json().get("data", {}).get("values", {})
        temperature = data.get("temperature")
        humidity = data.get("humidity")

        if temperature is None or humidity is None:
            logging.error("Incomplete data from API")
            return None, None

        return temperature, humidity

    except (RequestException, ValueError) as e:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        if is_dns_error(e):
            logging.error(
                f"[{timestamp}] DNS failure resolving api.tomorrow.io - will retry"
            )
        else:
            logging.error(
                f"[{timestamp}] Temperature request failed: {e}"
            )

        return None, None
        
        
def grab_forecast(tag="unknown"):
    _flush_if_units_changed()
    if _is_rate_limited():
        return []

    dt = datetime.now() - timedelta(days=1)

    try:
        s = get_session()
        resp = s.post(
            f"{TOMORROW_API_URL}/timelines",
            headers={
                "Accept-Encoding": "gzip",
                "accept": "application/json",
                "content-type": "application/json"
            },
            params={"apikey": TOMORROW_API_KEY},
            json={
                "location": TEMPERATURE_LOCATION,
                "units": _get_units(),
                "timezone": "auto",
                "dailyStartHour": 6,
                "fields": [
                    "temperatureMin",
                    "temperatureMax",
                    "weatherCodeFullDay",
                    "sunriseTime",
                    "sunsetTime",
                    "moonPhase"
                ],
                "timesteps": ["1d"],
                "endTime": (dt + timedelta(days=int(FORECAST_DAYS))).isoformat(), 
            },
            timeout=(5, 20)
        )

        if resp.status_code == 429:
            _set_rate_limited()
            return []

        resp.raise_for_status()

        data = resp.json().get("data", {})
        timelines = data.get("timelines", [])
        if not timelines:
            logging.error(f"[Forecast:{tag}] No timelines returned from API")
            return []

        intervals = timelines[0].get("intervals", [])
        if not intervals:
            logging.error(f"[Forecast:{tag}] Timelines returned but no intervals")
            return []
        # Commented out debug prints to keep the console clean
        #for i, day in enumerate(intervals):
        #    print(f"Day {i}:")
        #    print(json.dumps(day, indent=4)) 
        
        return intervals

    except RequestException as e:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        if is_dns_error(e):
            logging.error(
                f"[{timestamp}] [Forecast:{tag}] DNS failure resolving api.tomorrow.io - will retry"
            )
        else:
            logging.error(
                f"[{timestamp}] [Forecast:{tag}] API request failed: {e}"
            )
        return []
        
    except KeyError as e:
        logging.error(f"[Forecast:{tag}] Unexpected data format: {e}")
        return []


def get_temperature_cached() -> tuple:
    """Return (temperature, humidity) from module-level cache; fetch only when TTL expires."""
    global _temp_data, _temp_next_fetch
    now = datetime.now()
    if _temp_next_fetch is None or now >= _temp_next_fetch:
        result = grab_temperature_and_humidity()
        if result[0] is not None:
            _temp_data = result
            _temp_next_fetch = now + timedelta(seconds=_TEMP_TTL)
            _write_file_cache(_TEMP_CACHE_FILE, list(result))
        else:
            _temp_next_fetch = now + timedelta(seconds=60)
            if _temp_data is None:
                cached = _read_file_cache(_TEMP_CACHE_FILE)
                if cached:
                    _temp_data = tuple(cached)
    return _temp_data if _temp_data is not None else (None, None)


def get_forecast_cached() -> list:
    """Return forecast intervals from module-level cache; fetch only when TTL expires."""
    global _forecast_data, _forecast_next_fetch
    now = datetime.now()
    if _forecast_next_fetch is None or now >= _forecast_next_fetch:
        result = grab_forecast()
        if result:
            _forecast_data = result
            _forecast_next_fetch = now + timedelta(seconds=_FORECAST_TTL)
            _write_file_cache(_FORECAST_CACHE_FILE, result)
        else:
            _forecast_next_fetch = now + timedelta(seconds=300)
            if _forecast_data is None:
                cached = _read_file_cache(_FORECAST_CACHE_FILE)
                if cached:
                    _forecast_data = cached
    return _forecast_data or []
