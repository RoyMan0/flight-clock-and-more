"""
Calendar sources: ICS feed links and CalDAV logins.

  ics     Any .ics / webcal:// link — Google's "Secret address in iCal format",
          Outlook's published ICS link, iCloud public calendars, holiday/sports/
          school feeds. webcal:// is fetched as https://.
  caldav  A calendar account login — iCloud (Apple ID + app-specific password),
          Fastmail, Yahoo, Nextcloud… Written with requests + ElementTree (no
          lxml needed on the Pi). Discovery: current-user-principal →
          calendar-home-set → calendar list; events via a calendar-query REPORT.

Both feed the same parser: icalendar + recurring_ical_events expand repeating
events (RRULE, EXDATE, moved occurrences) inside the requested window. Those
libraries are imported lazily so a Pi that hasn't installed them yet shows a
clear error instead of the plugin failing to load.

Events: {uid, title, start, end, all_day, location, source, color}
  timed    start/end are aware datetimes in system local time
  all-day  start/end are dates (end exclusive, as in iCalendar)
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
USER_AGENT = "led-matrix-calendar/1.0"

CALDAV_PRESETS = {
    "icloud":   "https://caldav.icloud.com/",
    "fastmail": "https://caldav.fastmail.com/dav/",
    "yahoo":    "https://caldav.calendar.yahoo.com/",
}

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "a": "http://apple.com/ns/ical/",
}


class SourceError(Exception):
    """A source couldn't be read; the message is shown on the config page."""


def _libs():
    try:
        import icalendar                    # noqa: F401
        import recurring_ical_events        # noqa: F401
    except ImportError:
        raise SourceError("calendar libraries missing — run: pip install -r requirements.txt")
    return icalendar, recurring_ical_events


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------

def window(days_ahead: int) -> tuple:
    """Local midnight today → midnight after `days_ahead` days (1 = today only)."""
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=max(1, int(days_ahead)))


def _local(value):
    """Timed values → aware local datetime (floating times are local); dates unchanged."""
    if isinstance(value, datetime):
        return value.astimezone()
    return value


def parse_ics(data, start, end, source: str, color: str) -> list:
    icalendar, recurring = _libs()
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as e:
        raise SourceError(f"couldn't read the calendar data ({e})")
    events = []
    for ev in recurring.of(cal).between(start, end):
        if ev.name != "VEVENT":
            continue
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        try:
            dtstart = ev.decoded("DTSTART")
        except KeyError:
            continue
        all_day = isinstance(dtstart, date) and not isinstance(dtstart, datetime)
        if "DTEND" in ev:
            dtend = ev.decoded("DTEND")
        elif "DURATION" in ev:
            dtend = dtstart + ev.decoded("DURATION")
        else:
            dtend = dtstart + timedelta(days=1) if all_day else dtstart
        if all_day and isinstance(dtend, datetime):
            dtend = dtend.date()
        events.append({
            "uid": str(ev.get("UID", "")),
            "title": " ".join(str(ev.get("SUMMARY", "")).split()) or "(No title)",
            "start": _local(dtstart),
            "end": _local(dtend),
            "all_day": all_day,
            "location": " ".join(str(ev.get("LOCATION", "")).split()),
            "source": source,
            "color": color,
        })
    return events


# ------------------------------------------------------------------
# ICS links
# ------------------------------------------------------------------

def normalize_url(url: str) -> str:
    return re.sub(r"^webcals?://", "https://", (url or "").strip(), flags=re.I)


def fetch_ics(url: str, start, end, source: str, color: str) -> list:
    url = normalize_url(url)
    if not url.lower().startswith(("http://", "https://")):
        raise SourceError("no calendar link set")
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    if r.status_code in (401, 403, 404, 410):
        raise SourceError(f"the link returned HTTP {r.status_code} — check it's the full, current link")
    r.raise_for_status()
    if b"BEGIN:VCALENDAR" not in r.content[:4096]:
        raise SourceError("that link isn't a calendar feed (expected an .ics file)")
    return parse_ics(r.content, start, end, source, color)


# ------------------------------------------------------------------
# CalDAV
# ------------------------------------------------------------------

def _dav(method: str, url: str, auth, body: str, depth: str):
    """PROPFIND/REPORT, following redirects ourselves: requests would turn a 302
    into a GET and drop the login when iCloud redirects to its pNN- shard."""
    for _ in range(5):
        r = requests.request(method, url, data=body.encode("utf-8"), auth=auth, timeout=TIMEOUT,
                             allow_redirects=False, headers={
                                 "Depth": depth, "User-Agent": USER_AGENT,
                                 "Content-Type": "application/xml; charset=utf-8"})
        if r.status_code in (301, 302, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            continue
        if r.status_code in (401, 403):     # iCloud answers a bad login with either
            raise SourceError("login rejected — check the username and app-specific password")
        if r.status_code not in (200, 207):
            raise SourceError(f"server returned HTTP {r.status_code} for {method}")
        try:
            return url, ET.fromstring(r.content)
        except ET.ParseError:
            raise SourceError("server sent an unreadable response — is this a CalDAV address?")
    raise SourceError("too many redirects")


def _href(root, path: str):
    el = root.find(path, NS)
    return el.text.strip() if el is not None and el.text else None


_PRINCIPAL = ('<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>')
_HOME = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
         '<d:prop><c:calendar-home-set/></d:prop></d:propfind>')
_LIST = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">'
         '<d:prop><d:displayname/><d:resourcetype/><a:calendar-color/>'
         '<c:supported-calendar-component-set/></d:prop></d:propfind>')
_home_cache: dict = {}          # (server, user) → calendar-home URL


def _calendar_home(server_url: str, auth) -> str:
    key = (server_url, auth[0])
    if key in _home_cache:
        return _home_cache[key]
    base, root = None, None
    for candidate in (server_url, urljoin(server_url, "/.well-known/caldav")):
        try:
            base, root = _dav("PROPFIND", candidate, auth, _PRINCIPAL, "0")
            if _href(root, ".//d:current-user-principal/d:href"):
                break
        except SourceError as e:
            if "login rejected" in str(e):
                raise
            base, root = None, None
    principal = _href(root, ".//d:current-user-principal/d:href") if root is not None else None
    if not principal:
        raise SourceError("couldn't find your account on that server — check the server address")
    purl, root = _dav("PROPFIND", urljoin(base, principal), auth, _HOME, "0")
    home = _href(root, ".//c:calendar-home-set/d:href")
    if not home:
        raise SourceError("the server didn't report a calendar home")
    _home_cache[key] = urljoin(purl, home)
    return _home_cache[key]


def list_caldav_calendars(server_url: str, username: str, password: str) -> list:
    """[{href, name, color}] for every event calendar in the account."""
    if not (server_url and username and password):
        raise SourceError("enter the server, username and app-specific password")
    auth = (username.strip(), password)
    home = _calendar_home(server_url.strip(), auth)
    hurl, root = _dav("PROPFIND", home, auth, _LIST, "1")
    cals = []
    for resp in root.findall("d:response", NS):
        if resp.find(".//d:resourcetype/c:calendar", NS) is None:
            continue
        comps = [c.get("name") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
        if comps and "VEVENT" not in comps:
            continue                                     # reminders/tasks lists
        href = _href(resp, "d:href")
        name = _href(resp, ".//d:displayname") or "Calendar"
        color = (_href(resp, ".//a:calendar-color") or "")[:7]
        cals.append({"href": urljoin(hurl, href), "name": name,
                     "color": color if re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else ""})
    return cals


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fetch_caldav(src: dict, password: str, start, end) -> list:
    auth = ((src.get("username") or "").strip(), password or "")
    if not (src.get("server_url") and auth[0] and auth[1]):
        raise SourceError("enter the server, username and app-specific password")
    chosen = [c for c in (src.get("calendars") or []) if c.get("enabled", True)]
    if not src.get("calendars"):
        chosen = list_caldav_calendars(src["server_url"], *auth)     # nothing picked yet → all
    body = ('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT"><c:time-range start="{_utc(start)}" end="{_utc(end)}"/>'
            '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
    events = []
    for cal in chosen:
        _url, root = _dav("REPORT", cal["href"], auth, body, "1")
        color = cal.get("color") or src.get("color") or "#4a90d9"
        name = cal.get("name") or src.get("name") or "Calendar"
        for el in root.findall(".//c:calendar-data", NS):
            if el.text:
                events += parse_ics(el.text.encode("utf-8"), start, end, name, color)
    return events


# ------------------------------------------------------------------
# Public
# ------------------------------------------------------------------

def fetch_source(src: dict, secret: str, start, end) -> list:
    """secret = the ICS link (ics) or app-specific password (caldav)."""
    color = src.get("color") or "#4a90d9"
    name = (src.get("name") or "").strip() or "Calendar"
    if (src.get("type") or "ics") == "caldav":
        return fetch_caldav(src, secret, start, end)
    return fetch_ics(secret, start, end, name, color)
