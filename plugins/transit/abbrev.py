"""
Destination shortening for the narrow Dest column.

Plain truncation produces confusing results ("95th/Dan R"), so names are
shortened in stages, stopping as soon as the text fits:
  1. user aliases (always applied, even if the full name would fit)
  2. built-in per-agency aliases for well-known long destinations
  3. strip headsign noise ("W-Line Jeffco" → "Jeffco", "X via Y" → "X")
  4. word abbreviations (Avenue→Av, Station→Stn, Island→Isl, …)
  5. drop a trailing "Stn"/"TC"-style suffix
  6. keep the first part of compound names ("95th/Dan Ryan" → "95th")
  7. drop a trailing street type ("Flatbush Av" → "Flatbush")
  8. hard truncate (last resort)
"""

import re

BUILTIN_ALIASES = {
    "cta": {
        "95th/Dan Ryan":  "95th/Ryan",
        "Harlem/Lake":    "Harlem",
        "Ashland/63rd":   "Ashland63",
        "54th/Cermak":    "54th/Cermk",
        "Dempster-Skokie": "Skokie",
        "Cottage Grove":  "Cottage Gr",
        "UIC-Halsted":    "UIC",
        "Forest Park":    "Forest Pk",
    },
    "mta": {
        "Coney Island-Stillwell Av":      "Coney Isl",
        "Far Rockaway-Mott Av":           "Far Rkwy",
        "Rockaway Park-Beach 116 St":     "Rkwy Park",
        "Jamaica Center-Parsons/Archer":  "Jamaica Ct",
        "Van Cortlandt Park-242 St":      "242 St",
        "Inwood-207 St":                  "Inwood",
        "Ozone Park-Lefferts Blvd":       "Lefferts",
        "Flatbush Av-Brooklyn College":   "Flatbush",
        "Crown Hts-Utica Av":             "Utica Av",
        "Canarsie-Rockaway Pkwy":         "Canarsie",
        "Middle Village-Metropolitan Av": "Middle Vlg",
        "Astoria-Ditmars Blvd":           "Astoria",
        "Forest Hills-71 Av":             "Forest Hls",
        "Bay Ridge-95 St":                "Bay Ridge",
        "Wakefield-241 St":               "Wakefield",
        "Eastchester-Dyre Av":            "Dyre Av",
        "World Trade Center":             "WTC",
        "Times Sq-42 St":                 "Times Sq",
        "Grand Central-42 St":            "Grand Cent",
        "Flushing-Main St":               "Flushing",
        "Pelham Bay Park":                "Pelham Bay",
        "Brighton Beach":                 "Brighton",
        "Bedford Park Blvd":              "Bedford Pk",
        "Jamaica-179 St":                 "Jamaica179",
        "Harlem-148 St":                  "Harlem 148",
        "Norwood-205 St":                 "Norwood",
        "Rockaway Blvd":                  "Rkwy Blvd",
    },
    "rtd": {
        "Union Station":            "Union Stn",
        "Denver Airport Station":   "Airport",
        "Denver Airport":           "Airport",
        "RidgeGate Parkway Station": "RidgeGate",
        "Jefferson County Government Center-Golden Station": "Golden",
        "Jeffco Gov Ctr-Golden":    "Golden",
        "Wheat Ridge Ward Station": "Ward Rd",
        "Westminster Station":      "Westminstr",
        "Eastlake & 124th Station": "Eastlake",
        "18th&California Station":  "18th&Calif",
        "Downtown Boulder Station": "Boulder",
        "Civic Center Station":     "Civic Ctr",
    },
}

_WORDS = {
    "avenue": "Av", "ave": "Av", "street": "St", "boulevard": "Blvd",
    "parkway": "Pkwy", "road": "Rd", "drive": "Dr", "place": "Pl",
    "highway": "Hwy", "station": "Stn", "terminal": "Term", "center": "Ctr",
    "centre": "Ctr", "international": "Intl", "airport": "Arpt",
    "junction": "Jct", "square": "Sq", "heights": "Hts", "island": "Isl",
    "mount": "Mt", "fort": "Ft", "saint": "St", "university": "Univ",
    "college": "Coll", "hospital": "Hosp", "downtown": "Dtwn",
    "north": "N", "south": "S", "east": "E", "west": "W",
    "northbound": "NB", "southbound": "SB", "eastbound": "EB", "westbound": "WB",
    "government": "Gov", "county": "Co", "village": "Vlg", "rockaway": "Rkwy",
    "valley": "Vly", "ranch": "Rch", "crossing": "Xing", "crossroads": "Xrds",
    "springs": "Spgs", "plaza": "Plz", "commons": "Cmns", "memorial": "Mem",
}
_PHRASES = [
    (re.compile(r"\btransit (center|ctr)\b", re.I), "TC"),
    (re.compile(r"\bpark[\s-]*(n|and|&)[\s-]*ride\b", re.I), "PnR"),
]
_HEADSIGN_PREFIX = re.compile(r"^(?:[A-Z0-9]{1,2}[- ]Line\s+(?:to\s+)?|Train\s+to\s+|To\s+)", re.I)
_HEADSIGN_VIA = re.compile(r"\s+via\s+.*$", re.I)
_TRAILING_SUFFIX = re.compile(r"\s+(Stn|Sta|Station|TC|PnR|Term)$", re.I)
_TRAILING_STREET = re.compile(r"\s+(Av|St|Blvd|Rd|Pkwy|Dr|Pl|Hwy)$")


def _fits(s: str, max_chars: int) -> bool:
    return len(s) <= max_chars


_BRACKETED = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")   # "(Red Line)", "[ X Stop ]"


def _strip_noise(s: str) -> str:
    s = _BRACKETED.sub("", s).strip()
    return _HEADSIGN_VIA.sub("", _HEADSIGN_PREFIX.sub("", s))


def _abbreviate_words(s: str) -> str:
    for pat, rep in _PHRASES:
        s = pat.sub(rep, s)
    s = re.sub(r"\s*([/&])\s*", r"\1", s)            # "I-25 / Broadway" → "I-25/Broadway"
    words = []
    for w in s.split(" "):
        words.append(_WORDS.get(w.lower().rstrip("."), w))
    return " ".join(words)


_HIGHWAY = re.compile(r"^(I|US|SH|CO|Hwy)[- ]?\d+$", re.I)


def _first_part(s: str) -> str:
    """Keep the most distinctive part of a compound name ("95th/Dan Ryan" → "95th")."""
    for sep in ("/", " & ", "&", " - "):
        parts = [p.strip() for p in s.split(sep) if p.strip()]
        if len(parts) > 1:
            # "I-25/Broadway", "US36 & Broomfield" → the place name, not the highway
            if _HIGHWAY.match(parts[0]):
                return parts[1]
            if len(parts[0]) >= 3:
                return parts[0]
    # Split on a hyphen only between words, not inside "I-25"/"E-470"
    m = re.match(r"^(.{3,}?[A-Za-z])-([A-Za-z0-9].*)$", s)
    return m.group(1) if m else s


def _drop_last_word(s: str) -> str:
    head, _, _tail = s.rpartition(" ")
    return head if len(head) >= 4 else s


def shorten(name: str, max_chars: int, alias_set: str = "", user_aliases: dict = None) -> str:
    s = (name or "").strip()
    if user_aliases:
        for k, v in user_aliases.items():
            if k.strip().lower() == s.lower():
                return str(v)[:max_chars]
    if _fits(s, max_chars):
        return s

    builtin = {k.lower(): v for k, v in BUILTIN_ALIASES.get(alias_set, {}).items()}
    alias = builtin.get(s.lower())
    if alias and _fits(alias, max_chars):
        return alias

    s = _strip_noise(s)
    if _fits(s, max_chars):
        return s
    alias = builtin.get(s.lower())
    if alias and _fits(alias, max_chars):
        return alias

    steps = (
        _abbreviate_words,
        lambda x: _TRAILING_SUFFIX.sub("", x),
        _first_part,
        lambda x: _TRAILING_STREET.sub("", x),
        _drop_last_word,
    )
    for step in steps:
        s = step(s).strip()
        if _fits(s, max_chars):
            return s
    return s[:max_chars].rstrip()
