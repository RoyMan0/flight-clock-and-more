"""
SportsPlugin — shows live and recent scores from multiple leagues.

Data source: ESPN unofficial public API (no auth required).

Supported leagues: NFL, NBA, MLB, NHL, Soccer (multiple), NCAAF, NCAAB.

Display layout (64×32):
  Row 0–8:   League + game clock / status
  Row 9–20:  Team 1  |  Score 1
  Row 21–31: Team 2  |  Score 2

Cycles through each game for seconds_per_game, then signals cycle complete.
Live games (state=="in") can optionally trigger live_priority.
"""

import logging
import time
import threading
import requests
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

LEAGUE_URLS = {
    "nfl":              f"{ESPN_BASE}/football/nfl/scoreboard",
    "nba":              f"{ESPN_BASE}/basketball/nba/scoreboard",
    "mlb":              f"{ESPN_BASE}/baseball/mlb/scoreboard",
    "nhl":              f"{ESPN_BASE}/icehockey/nhl/scoreboard",
    "college_football": f"{ESPN_BASE}/football/college-football/scoreboard",
    "college_basketball": f"{ESPN_BASE}/basketball/mens-college-basketball/scoreboard",
}

# Soccer sub-leagues use a different URL pattern
def _soccer_url(league_id: str) -> str:
    return f"{ESPN_BASE}/soccer/{league_id}/scoreboard"

# Colors
COL_HEADER  = (180, 180, 180)
COL_LIVE    = (255, 80, 0)
COL_TEAM1   = (255, 255, 255)
COL_TEAM2   = (200, 200, 255)
COL_SCORE   = (255, 220, 0)
COL_FINAL   = (120, 120, 120)

def _load_font(size: int = 6):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except Exception:
        return ImageFont.load_default()

FONT_HD = _load_font(6)
FONT_TM = _load_font(7)
FONT_SC = _load_font(8)


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

    # ------------------------------------------------------------------
    # BasePlugin
    # ------------------------------------------------------------------

    def reset(self):
        self._current_idx = 0
        self._idx_start = time.monotonic()
        self._cycle_done = False

    def update(self):
        leagues_cfg = self.config.get("leagues", {})
        games = []

        # Standard leagues
        for league_id, url in LEAGUE_URLS.items():
            league_cfg = leagues_cfg.get(league_id, {})
            if not league_cfg.get("enabled", False):
                continue
            fetched = _fetch_league(url, league_cfg.get("name", league_id.upper()))
            games.extend(fetched)

        # Soccer sub-leagues
        soccer_cfg = leagues_cfg.get("soccer", {})
        if soccer_cfg.get("enabled", False):
            for sub in soccer_cfg.get("sub_leagues", []):
                fetched = _fetch_league(
                    _soccer_url(sub["id"]), sub.get("name", sub["id"])
                )
                games.extend(fetched)

        # Filter to favorite teams if configured (abbreviations, case-insensitive)
        favorites = {t.upper() for t in self.config.get("favorite_teams", [])}
        if favorites:
            games = [
                g for g in games
                if g["away"]["name"].upper() in favorites
                or g["home"]["name"].upper() in favorites
            ]

        # Sort: live first, then recent, then upcoming
        live = [g for g in games if g["state"] == "in"]
        done = [g for g in games if g["state"] == "post"]
        pre  = [g for g in games if g["state"] == "pre"]
        games = live + done + pre

        with self._lock:
            self._games = games
            self._has_live = len(live) > 0

        log.debug(f"[sports] Updated: {len(games)} games ({len(live)} live)")

    def draw(self) -> bool:
        with self._lock:
            games = list(self._games)
            has_live = self._has_live

        if not games:
            return False

        spr = float(self.config.get("seconds_per_game", 7))
        now = time.monotonic()

        # Advance to next game when timer expires
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

def _fetch_league(url: str, league_name: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "LEDMatrix/1.0"})
        if r.status_code != 200:
            return []
        data = r.json()
        events = data.get("events", [])
        games = []
        for event in events:
            game = _parse_event(event, league_name)
            if game:
                games.append(game)
        return games
    except Exception as e:
        log.debug(f"[sports] Fetch error ({league_name}): {e}")
        return []


def _parse_event(event: dict, league: str) -> Optional[dict]:
    try:
        competitions = event.get("competitions", [])
        if not competitions:
            return None
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        status = event.get("status", {})
        state = status.get("type", {}).get("state", "pre")  # pre / in / post
        clock = status.get("displayClock", "")
        period = status.get("period", 0)
        status_detail = status.get("type", {}).get("shortDetail", "")

        teams = []
        for c in competitors[:2]:
            team = c.get("team", {})
            score = c.get("score", "0")
            home_away = c.get("homeAway", "")
            abbrev = team.get("abbreviation", team.get("displayName", "?"))[:6]
            color = team.get("color", "")
            teams.append({
                "name": abbrev,
                "score": score,
                "home_away": home_away,
                "color": _hex_to_rgb(color),
            })

        # Ensure home team is listed second
        if len(teams) == 2 and teams[0].get("home_away") == "home":
            teams = [teams[1], teams[0]]

        return {
            "league": league,
            "state": state,
            "clock": clock,
            "period": period,
            "status_detail": status_detail,
            "away": teams[0],
            "home": teams[1],
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

def _render_game(game: dict) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    state = game["state"]
    is_live = state == "in"
    is_final = state == "post"

    # Header: league + status
    header_color = COL_LIVE if is_live else (COL_FINAL if is_final else COL_HEADER)
    league_str = game["league"]
    if is_live:
        clock_str = f"P{game['period']} {game['clock']}"
        header = f"{league_str} {clock_str}"
    elif is_final:
        header = f"{league_str} FINAL"
    else:
        header = f"{league_str} {game['status_detail'][:12]}"

    draw.text((1, 1), header[:16], font=FONT_HD, fill=header_color)

    # Divider line
    draw.line([(0, 10), (MATRIX_W, 10)], fill=(40, 40, 40))

    # Away team (row 1)
    away = game["away"]
    team_col = _clamp_color(away["color"]) or COL_TEAM1
    draw.text((1, 12), away["name"], font=FONT_TM, fill=team_col)
    score_str = str(away["score"])
    draw.text((MATRIX_W - len(score_str) * 6 - 1, 12), score_str, font=FONT_SC, fill=COL_SCORE)

    # Home team (row 2)
    home = game["home"]
    team_col2 = _clamp_color(home["color"]) or COL_TEAM2
    draw.text((1, 22), home["name"], font=FONT_TM, fill=team_col2)
    score_str2 = str(home["score"])
    draw.text((MATRIX_W - len(score_str2) * 6 - 1, 22), score_str2, font=FONT_SC, fill=COL_SCORE)

    return img


def _clamp_color(rgb: tuple) -> Optional[tuple]:
    """Ensure color is visible (not too dark)."""
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
