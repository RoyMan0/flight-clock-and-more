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
_FONTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "fonts")
)

# Module-level logo cache: "LEAGUE:ABBREV" → PIL RGBA image or None
_logo_cache: dict = {}
_logo_failed: set = set()


# ------------------------------------------------------------------
# BDF pixel-font renderer  (same font family as temperature/clock scenes)
# ------------------------------------------------------------------

def _parse_bdf(path: str) -> dict:
    """Parse BDF font file → {char_code: {dw, bbw, bbh, rows}}."""
    glyphs: dict = {}
    cur: dict = {}
    rows: list = []
    reading = False
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ENCODING"):
                    cur = {"code": int(line.split()[1])}
                elif line.startswith("DWIDTH"):
                    cur["dw"] = int(line.split()[1])
                elif line.startswith("BBX"):
                    p = line.split()
                    cur.update(bbw=int(p[1]), bbh=int(p[2]), xoff=int(p[3]), yoff=int(p[4]))
                elif line == "BITMAP":
                    reading, rows = True, []
                elif line == "ENDCHAR" and reading:
                    cur["rows"] = rows
                    glyphs[cur["code"]] = cur
                    reading = False
                elif reading:
                    rows.append(int(line, 16))
    except Exception as e:
        log.warning(f"[sports] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_width(text: str, font: dict) -> int:
    return sum(font[ord(c)]["dw"] for c in text if ord(c) in font)


def _bdf_draw(img: Image.Image, x: int, y: int, text: str,
              font: dict, color: tuple, outline: bool = False):
    """Stamp BDF glyphs directly into img pixels. y = top of character cell."""
    if not font:
        return
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = font.get(ord(ch))
        if g is None:
            cx += 4
            continue
        bbw = g["bbw"]
        total_bits = ((bbw + 7) // 8) * 8
        if outline:
            for ri, rv in enumerate(g["rows"]):
                for bi in range(bbw):
                    if (rv >> (total_bits - 1 - bi)) & 1:
                        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                            ox, oy = cx + bi + dx, y + ri + dy
                            if 0 <= ox < iw and 0 <= oy < ih:
                                px[ox, oy] = (0, 0, 0)
        for ri, rv in enumerate(g["rows"]):
            py = y + ri
            if py < 0 or py >= ih:
                continue
            for bi in range(bbw):
                if (rv >> (total_bits - 1 - bi)) & 1:
                    ox = cx + bi
                    if 0 <= ox < iw:
                        px[ox, py] = color
        cx += g["dw"]


# 4x6 BDF: 4px-wide × 6px-tall characters — same family as the clock/temp scenes
_BDF = _parse_bdf(os.path.join(_FONTS_DIR, "4x6.bdf"))
_BDF_H   = 6   # character cell height of 4x6.bdf
HEADER_H = 7   # header zone: 6px text + 1px gap
_SCR_H   = 7   # score zone:  6px text + 1px bottom margin


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
            games.extend(_fetch_league(url, lcfg.get("name", league_id.upper()), dates, league_id))

        soccer_cfg = leagues_cfg.get("soccer", {})
        if soccer_cfg.get("enabled", False):
            for sub in soccer_cfg.get("sub_leagues", []):
                games.extend(_fetch_league(
                    _soccer_url(sub["id"]), sub.get("name", sub["id"]), dates, "soccer"
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

def _fetch_league(url: str, league_name: str, dates: str, league_id: str = "") -> list[dict]:
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
            game = _parse_event(event, league_name, league_id)
            if game:
                games.append(game)
        return games
    except Exception as e:
        log.debug(f"[sports] Fetch error ({league_name}): {e}")
        return []


def _parse_event(event: dict, league: str, league_id: str = "") -> Optional[dict]:
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

        # Situation block — used by baseball (balls/strikes/outs) and
        # football (down/distance/possession).  Safe to capture for all sports.
        raw_sit = comp.get("situation", {}) or {}
        situation = {
            # Baseball
            "balls":     raw_sit.get("balls",     0),
            "strikes":   raw_sit.get("strikes",   0),
            "outs":      raw_sit.get("outs",      0),
            # Football
            "down":       raw_sit.get("down",       0),
            "distance":   raw_sit.get("distance",   0),
            "possession": raw_sit.get("possession", ""),  # team id of possessing team
        }

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
                "team_id":  str(team.get("id", "")),   # used for possession matching
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
            "league_id":     league_id,
            "state":         state,
            "clock":         clock,
            "period":        period,
            "status_detail": status_detail,
            "situation":     situation,
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

def _mlb_status(game: dict) -> str:
    """Build a baseball-specific status string: 'T5 S1-B2 O2'."""
    detail = (game.get("status_detail") or "").lower()
    period = game["period"]

    if detail.startswith("top") or detail.startswith("t "):
        half = "T"
    elif detail.startswith("bot") or detail.startswith("b "):
        half = "B"
    elif detail.startswith("mid"):
        return f"MID{period}"
    else:
        return f"END{period}"

    sit = game.get("situation", {})
    s = sit.get("strikes", 0)
    b = sit.get("balls",   0)
    o = sit.get("outs",    0)
    return f"{half}{period} S{s}-B{b} O{o}"


_FOOTBALL_LEAGUES    = {"nfl", "college_football"}
_TIMED_SPORT_LEAGUES = {"nba", "college_basketball", "nhl"}


def _timed_status(game: dict) -> str:
    """Period + clock for NBA, NCAAB, and NHL."""
    period = game["period"]
    clock  = game["clock"] or ""
    lid    = game.get("league_id", "")

    if lid == "nba":
        label = f"Q{period}" if period <= 4 else "OT"
    elif lid == "college_basketball":
        label = f"H{period}" if period <= 2 else "OT"
    else:  # nhl
        if period <= 3:
            label = f"P{period}"
        elif period == 4:
            label = "OT"
        else:
            label = "SO"   # shootout

    return f"{label} {clock}" if clock else label


def _soccer_status(game: dict) -> str:
    """Half label + elapsed clock for soccer, e.g. '1st 45+2'."""
    period = game["period"]
    clock  = game["clock"] or ""

    if period == 1:
        half = "1st"
    elif period == 2:
        half = "2nd"
    elif period in (3, 4):
        half = "OT"
    else:
        half = "PKS"   # penalty shootout

    return f"{half} {clock}" if clock else half


def _football_status(game: dict) -> str:
    """Build football status: 'Q4 2:14 4&1'. Falls back to clock-only if too long."""
    period = game["period"]
    clock  = game["clock"] or ""
    detail = (game.get("status_detail") or "")

    if not period or not clock:
        return detail[:10]

    if "half" in detail.lower():
        return "HALF"

    qtr = f"Q{period}" if period <= 4 else "OT"

    sit  = game.get("situation", {})
    down = sit.get("down", 0)
    dist = sit.get("distance", 0)

    if down:
        full = f"{qtr} {clock} {down}&{dist}"
        if len(full) <= 12:   # ~48px — fits alongside a 3-char league name
            return full

    return f"{qtr} {clock}"


def _render_game(game: dict) -> Image.Image:
    img  = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    state    = game["state"]
    is_live  = state == "in"
    is_final = state == "post"
    lid      = game.get("league_id", "")

    status_col = COL_LIVE if is_live else (COL_FINAL if is_final else COL_PRE)
    if is_live and lid == "mlb":
        status_str = _mlb_status(game)
    elif is_live and lid in _FOOTBALL_LEAGUES:
        status_str = _football_status(game)
    elif is_live and lid in _TIMED_SPORT_LEAGUES:
        status_str = _timed_status(game)
    elif is_live and lid == "soccer":
        status_str = _soccer_status(game)
    elif is_live:
        status_str = f"P{game['period']} {game['clock']}"
    elif is_final:
        status_str = "FINAL"
    else:
        status_str = game["status_detail"][:14]

    # ── Header: BDF pixel text on clean black, y=1 (centered in HEADER_H zone)
    _bdf_draw(img, 1, 1, game["league"][:7], _BDF, COL_HEADER)
    sw = _bdf_width(status_str, _BDF)
    _bdf_draw(img, MATRIX_W - sw - 1, 1, status_str, _BDF, status_col)

    # ── Logo slots fill the space below header ────────────────────────
    _draw_slot(img, draw, game["away"], slot_x=0,      game=game)
    _draw_slot(img, draw, game["home"], slot_x=SLOT_W, game=game)

    score_y = MATRIX_H - _SCR_H

    # ── Batting indicator (MLB): < away batting, > home batting ───────
    if is_live and lid == "mlb":
        detail = (game.get("status_detail") or "").lower()
        if detail.startswith("top") or detail.startswith("t "):
            _bdf_draw(img, 30, score_y, "<", _BDF, (255, 200, 0))
        elif detail.startswith("bot") or detail.startswith("b "):
            _bdf_draw(img, 30, score_y, ">", _BDF, (255, 200, 0))

    # ── Possession indicator (football): < away ball, > home ball ─────
    elif is_live and lid in _FOOTBALL_LEAGUES:
        poss = game.get("situation", {}).get("possession", "")
        if poss:
            if game["away"]["team_id"] == poss:
                _bdf_draw(img, 30, score_y, "<", _BDF, (255, 200, 0))
            elif game["home"]["team_id"] == poss:
                _bdf_draw(img, 30, score_y, ">", _BDF, (255, 200, 0))

    return img


def _draw_slot(img: Image.Image, draw,
               team: dict, slot_x: int, game: dict):
    """Render one team's logo + score into its 32-wide slot."""
    MARGIN = 2

    sy         = MATRIX_H - _SCR_H     # y where score text starts
    logo_top   = HEADER_H              # first pixel available for logo
    logo_max_w = SLOT_W - MARGIN * 2
    logo_max_h = sy - logo_top - 1    # height available for logo

    logo = _logo_cache.get(_logo_cache_key(game["league"], team["name"]))

    if logo and logo_max_h > 0:
        fitted = _fit_logo(logo, logo_max_w, logo_max_h)
        lx = slot_x + (SLOT_W - fitted.width) // 2
        ly = logo_top + (logo_max_h - fitted.height) // 2
        img.paste(fitted, (lx, ly), fitted)
    else:
        col = _clamp_color(team["color"]) or COL_HEADER
        label = team["name"][:5]
        tw = _bdf_width(label, _BDF)
        ty = logo_top + max(0, (logo_max_h - _BDF_H) // 2)
        _bdf_draw(img, slot_x + (SLOT_W - tw) // 2, ty, label, _BDF, col)

    # Score: BDF pixel text with black outline so it reads over logos
    score_str = str(team["score"]) if (game["state"] != "pre" and team["score"] != "") else "-"
    sc_w = _bdf_width(score_str, _BDF)
    sx   = slot_x + (SLOT_W - sc_w) // 2
    _bdf_draw(img, sx, sy, score_str, _BDF, COL_SCORE, outline=True)


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
