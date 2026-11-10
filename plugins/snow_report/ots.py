"""
OnTheSnow resort snow reports — read from the public resort pages.

OnTheSnow has no free API (the partner API is licensed), but each resort's
public "skireport" page embeds the full report as Next.js flight data
(`self.__next_f.push([1, "…"])` chunks: rows of `<hex id>:<json>`, where
"$<id>" strings point at other rows). robots.txt allows all pages. We fetch
one page per resort about once an hour with an honest User-Agent.

Editions: onthesnow.com (North America + Japan, Australia/NZ, South America)
and onthesnow.co.uk (Europe). A resort id is "<edition>/<region>/<slug>",
e.g. "com/colorado/arapahoe-basin-ski-area" or "uk/valais/verbier".

All depths and snowfall are centimetres, temperatures °C (as on the page).
"""

import json
import logging
import re
from datetime import date

import requests

log = logging.getLogger(__name__)

EDITIONS = {"com": "https://www.onthesnow.com", "uk": "https://www.onthesnow.co.uk"}
HEADERS = {"User-Agent": "LEDMatrix/1.0 (personal LED sign; one request per resort per hour)"}
TIMEOUT = 15


class SnowError(Exception):
    pass


# ------------------------------------------------------------------
# Next.js flight data
# ------------------------------------------------------------------

_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
_ROW_RE = re.compile(r'(?:^|\n)([0-9a-f]+):(?=[\[{"])')
_REF_RE = re.compile(r"\$[0-9a-f]+")


def rsc_rows(html: str) -> dict:
    text = "".join(json.loads('"' + c + '"') for c in _CHUNK_RE.findall(html))
    rows, dec = {}, json.JSONDecoder()
    for m in _ROW_RE.finditer(text):
        try:
            rows[m.group(1)], _ = dec.raw_decode(text, m.end())
        except ValueError:
            pass
    return rows


def resolve(v, rows: dict, depth: int = 0):
    if depth > 10:
        return v
    if isinstance(v, str) and _REF_RE.fullmatch(v):
        return resolve(rows.get(v[1:]), rows, depth + 1)
    if isinstance(v, dict):
        return {k: resolve(x, rows, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, rows, depth + 1) for x in v]
    return v


def page_url(resort_id: str, page: str = "skireport") -> str:
    edition, path = resort_id.split("/", 1)
    if edition not in EDITIONS:
        raise SnowError(f"Unknown resort id {resort_id!r}")
    return f"{EDITIONS[edition]}/{path}/{page}"


def _get(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SnowError(f"Couldn't reach OnTheSnow ({type(e).__name__})")
    if r.status_code == 404:
        raise SnowError("Resort page not found on OnTheSnow")
    if r.status_code != 200:
        raise SnowError(f"OnTheSnow returned {r.status_code}")
    return r.text


# ------------------------------------------------------------------
# Snow report
# ------------------------------------------------------------------

def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_report(html: str, slug: str) -> dict:
    rows = rsc_rows(html)
    raw = next((r for r in rows.values()
                if isinstance(r, dict) and r.get("slug") == slug and "reportStatus" in r), None)
    if raw is None:
        raise SnowError("Couldn't read the snow report (OnTheSnow page layout may have changed)")
    o = resolve(raw, rows)
    snow = o.get("snow") or o.get("depths") or {}
    runs = o.get("runs") or (o.get("terrain") or {}).get("runs") or {}
    lifts = o.get("lifts") or {}
    status = o.get("status") or {}
    runs_total, runs_open, runs_pct = _num(runs.get("total")), _num(runs.get("open")), _num(runs.get("openPercent"))
    if runs_open is None and runs_pct is not None and runs_total:
        runs_open = round(runs_total * runs_pct / 100)
    if runs_pct is None and runs_open is not None and runs_total:
        runs_pct = 100 * runs_open / runs_total

    today = date.today().isoformat()
    days = []
    for w in o.get("weather") or []:
        if (w.get("reportedDate") or "") >= today:
            days.append({"date": w["reportedDate"], "high": _num(w.get("tempHigh")), "low": _num(w.get("tempLow")),
                         "snow": _num(w.get("snowfall")) or 0.0, "type": w.get("type") or ""})
    cw = o.get("currentWeather") or {}
    band = cw.get("mid") or cw.get("base") or cw.get("summit") or {}
    return {
        "name": o.get("title") or slug,
        "short": o.get("title_short") or o.get("title") or slug,
        "open": status.get("openFlag") == 1,
        "opening_date": status.get("openingDate"),
        "closing_date": status.get("closingDate"),
        "updated_at": o.get("updatedAt"),
        "base_cm": _num(snow.get("base")) if snow.get("base") is not None else _num(snow.get("middle")),
        "summit_cm": _num(snow.get("summit")),
        "new_cm": {"24": _num(snow.get("last24")), "48": _num(snow.get("last48")), "72": _num(snow.get("last72"))},
        "lifts_open": _num(lifts.get("open")), "lifts_total": _num(lifts.get("total")),
        "runs_open": runs_open, "runs_total": runs_total, "runs_pct": runs_pct,
        "weather": {"type": cw.get("type") or (days[0]["type"] if days else ""),
                    "high": _num(band.get("max")), "low": _num(band.get("min"))},
        "forecast": days[:7],
        "lat": _num(o.get("latitude")), "lon": _num(o.get("longitude")),
    }


def fetch_report(resort_id: str) -> dict:
    slug = resort_id.rstrip("/").rsplit("/", 1)[-1]
    rep = parse_report(_get(page_url(resort_id)), slug)
    rep["id"] = resort_id
    return rep


# ------------------------------------------------------------------
# Resort listings (used by scripts/build_snow_resorts.py)
# ------------------------------------------------------------------

def list_region(edition: str, region: str) -> list:
    """Resorts on a region's /ski-resorts page: [{id, name, short, region, country}]."""
    rows = rsc_rows(_get(f"{EDITIONS[edition]}/{region}/ski-resorts"))
    out, seen = [], set()
    for r in rows.values():
        # Resort cards on listing pages: {uuid, title, title_short, slug, region (slug), region_en, country_code}
        if not (isinstance(r, dict) and r.get("slug") and r.get("title") and "region_id" in r
                and isinstance(r.get("region"), str)):
            continue
        rid = f"{edition}/{r['region']}/{r['slug']}"
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "name": r["title"], "short": r.get("title_short") or r["title"],
                    "region": r.get("region_en") or r["region"], "country": r.get("country_code") or ""})
    return out
