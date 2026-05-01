"""
SportsPlugin — shows live and recent scores from multiple leagues.

Data source: ESPN unofficial public API (no auth required).
Logos: downloaded from ESPN CDN and cached in assets/sports/logos/.

Display layout (64×32):
  Row 0–6:   League name (left) + status/clock (right)
  Row 7:     Divider line
  Row 8–31:  Away logo (left 32px slot) | Home logo (right 32px slot)
             Scores overlaid at bottom of each slot with black stroke
"""

import io
import logging
import os
import time
import threading
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32
SLOT_W   = 32   # each team occupies half the width

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

LEAGUE_URLS = {
    "nfl":                f"{ESPN_BASE}/football/nfl/scoreboard",
    "nba":                f"{ESPN_BASE}/basketball/nba/scoreboard",
    "mlb":                f"{ESPN_BASE}/baseball/mlb/scoreboard",
    "nhl":                f"{ESPN_BASE}/hockey/nhl/scoreboard",
    "college_football":   f"{ESPN_BASE}/football/college-football/scoreboard",
    "college_basketball": f"{ESPN_BASE}/basketball/mens-college-basketball/scoreboard",
}

def _soccer_url(league_id: str) -> str:
    return f"{ESPN_BASE}/soccer/{league_id}/scoreboard"

COL_HEADER = (180, 180, 180)
COL_LIVE   = (255, 80,  0)
COL_FINAL  = (120, 120, 120)
COL_PRE    = (100, 180, 255)
COL_SCORE  = (255, 220, 0)

LOGO_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "assets", "sports", "logos")
)

# Module-level logo cache: "LEAGUE:ABBREV" → PIL RGBA image or None
_logo_cache: dict = {}
_logo_failed: set = set()


def _load_font(size: int):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()

FONT_HDR = _load_font(5)   # header: league + status
FONT_SCR = _load_font(7)   # score numbers (slightly smaller to reclaim space)
FONT_TXT = _load_font(6)   # team name fallback

def _measure_font_h(font) -> int:
    try:
        bb = font.getbbox("0")
        return bb[3] - bb[1]
    except Exception:
        return 8

_SCR_H  = _measure_font_h(FONT_SCR) + 2   # score zone height (stroke + 1px margin)
_HDR_H  = _measure_font_h(FONT_HDR) + 2   # header zone height (text + 2px padding)
HEADER_H = max(_HDR_H, 7)                  # at least 7px reserved for header row


# ------------------------------------------------------------------
# Date helpers
# ------------------------------------------------------------------

def _eastern_now() -> datetime:
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 11 else -5   # approximate DST
    return utc + timedelta(hours=offset)


def _dates_param() -> str:
    """ESPN 'dates' param: yesterday–today in Eastern time."""
    et = _eastern_now()
    today     = et.strftime("%Y%m%d")
    yesterday = (et - timedelta(days=1)).strftime("%Y%m%d")
    return f"{yesterday}-{today}"


# ------------------------------------------------------------------
# Logo helpers
# ------------------------------------------------------------------

def _logo_cache_key(league: str, abbrev: str) -> str:
    return f"{league}:{abbrev}"


def _logo_disk_path(league: str, abbrev: str) -> str:
    return os.path.join(LOGO_DIR, league.lower(), f"{abbrev.upper()}.png")


def _get_logo(logo_url: str, league: str, abbrev: str) -> Optional[Image.Image]:
    key = _logo_cache_key(league, abbrev)
    if key in _logo_cache:
        return _logo_cache[key]
    if key in _logo_failed:
        return None

    path = _logo_disk_path(league, abbrev)
    if os.path.exists(path):
        try:
            img = Image.open(path).convert("RGBA")
            _logo_cache[key] = img
            return img
        except Exception:
            pass

    if logo_url:
        try:
            r = requests.get(logo_url, timeout=8, headers={"User-Agent": "LEDMatrix/1.0"})
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "image" in ct:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(r.content)
                img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                _logo_cache[key] = img
                return img
        except Exception as e:
            log.debug(f"[sports] Logo download failed ({abbrev}): {e}")

    _logo_failed.add(key)
    _logo_cache[key] = None
    return None


def _fit_logo(logo: Image.Image, max_w: int, max_h: int) -> Image.Image:
    result = logo.copy()
    result.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    return result


# ------------------------------------------------------------------
# Plugin class
# ------------------------------------------------------------------

class SportsPlugin(BasePlugin):
    PLUGIN_ID = "sports"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._games: list[dict] = []
        self._current_idx: int = 0
        self._idx_start: float = 0.0
        self._lock = threading.Lock()
        self._cycle_done = False
        self._has_live = False

    def reset(self):
        self._current_idx = 0
        self._idx_start = time.monotonic()
        self._cycle_done = False

    def update(self):
        leagues_cfg = self.config.get("leagues", {})
        dates = _dates_param()
        games = []

        for league_id, url in LEAGUE_URLS.items():
            lcfg = leagues_cfg.get(league_id, {})
            if not lcfg.get("enabled", False):
                continue
            games.extend(_fetch_league(url, lcfg.get("name", league_id.upper()), dates))

        soccer_cfg = leagues_cfg.get("soccer", {})
        if soccer_cfg.get("enabled", False):
            for sub in soccer_cfg.get("sub_leagues", []):
                games.extend(_fetch_league(
                    _soccer_url(sub["id"]), sub.get("name", sub["id"]), dates
                ))

        # Filter upcoming if disabled
        if not self.config.get("show_upcoming", False):
            games = [g for g in games if g["state"] != "pre"]

        # Favorite teams filter
        favorites = {t.upper() for t in self.config.get("favorite_teams", [])}
        show_all  = {s.upper() for s in self.config.get("show_all_leagues", [])}
        if favorites:
            def _matches(game):
                if game["league"].upper() in show_all:
                    return True
                lg = game["league"].upper()
                for side in ("away", "home"):
                    ab = game[side]["name"].upper()
                    if f"{lg}:{ab}" in favorites or ab in favorites:
                        return True
                return False
            games = [g for g in games if _matches(g)]

        live = [g for g in games if g["state"] == "in"]
        done = [g for g in games if g["state"] == "post"]
        pre  = [g for g in games if g["state"] == "pre"]
        games = live + done + pre

        # Prefetch logos in this background thread so draw() never blocks on I/O
        for game in games:
            for side in ("away", "home"):
                t = game[side]
                _get_logo(t.get("logo_url", ""), game["league"], t["name"])

        with self._lock:
            self._games  = games
            self._has_live = bool(live)

        log.debug(f"[sports] Updated: {len(games)} games ({len(live)} live)")

    def draw(self) -> bool:
        with self._lock:
            games = list(self._games)

        if not games:
            return False

        spr = float(self.config.get("seconds_per_game", 7))
        now = time.monotonic()

        if now - self._idx_start >= spr:
            self._current_idx += 1
            self._idx_start = now
            if self._current_idx >= len(games):
                self._cycle_done = True
                self._current_idx = 0

        game = games[min(self._current_idx, len(games) - 1)]
        frame = _render_game(game)
        _push_pil_to_canvas(frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return self._cycle_done

    def has_live_priority(self) -> bool:
        return self.config.get("live_priority", False)

    def has_live_content(self) -> bool:
        return self._has_live


# ------------------------------------------------------------------
# ESPN API
# ------------------------------------------------------------------

def _fetch_league(url: str, league_name: str, dates: str) -> list[dict]:
    try:
        r = requests.get(
            url,
            params={"dates": dates},
            timeout=10,
            headers={"User-Agent": "LEDMatrix/1.0"},
        )
        if r.status_code != 200:
            return []
        games = []
        for event in r.json().get("events", []):
            game = _parse_event(event, league_name)
            if game:
                games.append(game)
        return games
    except Exception as e:
        log.debug(f"[sports] Fetch error ({league_name}): {e}")
        return []


def _parse_event(event: dict, league: str) -> Optional[dict]:
    try:
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        status = event.get("status", {})
        state         = status.get("type", {}).get("state", "pre")
        clock         = status.get("displayClock", "")
        period        = status.get("period", 0)
        status_detail = status.get("type", {}).get("shortDetail", "")

        teams = []
        for c in competitors[:2]:
            team = c.get("team", {})
            abbrev = team.get("abbreviation", team.get("shortDisplayName", "?"))[:6]

            logo_url = team.get("logo", "")
            if not logo_url:
                logos = team.get("logos", [])
                if logos:
                    logo_url = logos[0].get("href", "")

            teams.append({
                "name":     abbrev,
                "score":    c.get("score", ""),
                "home_away": c.get("homeAway", ""),
                "color":    _hex_to_rgb(team.get("color", "")),
                "logo_url": logo_url,
            })

        # Ensure away is index 0, home is index 1
        if len(teams) == 2 and teams[0].get("home_away") == "home":
            teams = [teams[1], teams[0]]

        return {
            "league":        league,
            "state":         state,
            "clock":         clock,
            "period":        period,
            "status_detail": status_detail,
            "away":          teams[0],
            "home":          teams[1],
        }
    except Exception:
        return None


def _hex_to_rgb(hex_color: str) -> tuple:
    try:
        h = hex_color.lstrip("#")
        if len(h) == 6:
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return (180, 180, 180)


# ------------------------------------------------------------------
# Rendering
# ------------------------------------------------------------------

def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _render_game(game: dict) -> Image.Image:
    img  = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    state    = game["state"]
    is_live  = state == "in"
    is_final = state == "post"

    status_col = COL_LIVE if is_live else (COL_FINAL if is_final else COL_PRE)
    if is_live:
        status_str = f"P{game['period']} {game['clock']}"
    elif is_final:
        status_str = "FINAL"
    else:
        status_str = game["status_detail"][:14]

    # ── Header drawn first on clean black so logos never overlap it ──
    draw.text((1, 1), game["league"][:7], font=FONT_HDR, fill=COL_HEADER,
              stroke_width=1, stroke_fill=(0, 0, 0))
    sw = _tw(draw, status_str, FONT_HDR)
    draw.text((MATRIX_W - sw - 1, 1), status_str, font=FONT_HDR, fill=status_col,
              stroke_width=1, stroke_fill=(0, 0, 0))

    # ── Logo slots occupy the space below the header ─────────────────
    _draw_slot(img, draw, game["away"], slot_x=0,      game=game)
    _draw_slot(img, draw, game["home"], slot_x=SLOT_W, game=game)

    return img


def _draw_slot(img: Image.Image, draw: ImageDraw.ImageDraw,
               team: dict, slot_x: int, game: dict):
    """Render one team's logo + score into its 32-wide slot."""
    MARGIN = 2

    # Logo zone: starts just below the header row, ends just above the score row
    sy         = MATRIX_H - _SCR_H          # y where score text starts
    logo_top   = HEADER_H                   # first pixel available for logo
    logo_max_w = SLOT_W - MARGIN * 2
    logo_max_h = sy - logo_top - 1          # height available for logo

    logo = _logo_cache.get(_logo_cache_key(game["league"], team["name"]))

    if logo and logo_max_h > 0:
        fitted = _fit_logo(logo, logo_max_w, logo_max_h)
        lx = slot_x + (SLOT_W - fitted.width) // 2
        ly = logo_top + (logo_max_h - fitted.height) // 2
        img.paste(fitted, (lx, ly), fitted)
    else:
        col = _clamp_color(team["color"]) or COL_HEADER
        label = team["name"][:5]
        tw = _tw(draw, label, FONT_TXT)
        try:
            txt_h = FONT_TXT.getbbox("A")[3]
        except Exception:
            txt_h = 6
        ty = logo_top + max(0, (logo_max_h - txt_h) // 2)
        draw.text((slot_x + (SLOT_W - tw) // 2, ty), label, font=FONT_TXT, fill=col)

    # Score at the bottom; black stroke keeps it readable over any logo bleed
    score_str = str(team["score"]) if (game["state"] != "pre" and team["score"] != "") else "-"
    sc_w = _tw(draw, score_str, FONT_SCR)
    sx   = slot_x + (SLOT_W - sc_w) // 2
    draw.text((sx, sy), score_str, font=FONT_SCR, fill=COL_SCORE,
              stroke_width=1, stroke_fill=(0, 0, 0))


def _clamp_color(rgb: tuple) -> Optional[tuple]:
    if not rgb or sum(rgb) < 60:
        return None
    return rgb


def _push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))
