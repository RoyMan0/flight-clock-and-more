"""
Chicago Transit Authority providers.

Trains: CTA Train Tracker API (free key: transitchicago.com/developers/traintrackerapply)
        Station/platform list from the City of Chicago open-data "L" stops dataset (no key).
Buses:  CTA Bus Tracker API v2 (free key: ctabustracker.com → Developers).

All CTA timestamps are local Chicago time with no zone suffix.
"""

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from plugins.transit.providers.base import (
    HTTP_HEADERS, Arrival, ProviderConfigError, TransitProvider, hex_to_rgb,
    merge_sequence, order_by_line, orient_line,
)

log = logging.getLogger(__name__)

_CHI = ZoneInfo("America/Chicago")
_DATA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "transit")
)

TRAIN_API = "https://lapi.transitchicago.com/api/1.0/ttarrivals.aspx"
L_STOPS_URL = "https://data.cityofchicago.org/resource/8pix-ypme.json?$limit=2000"
BUS_API = "https://www.ctabustracker.com/bustime/api/v2"
# Station order per line, generated from CTA GTFS by scripts/build_cta_rail_order.py
RAIL_ORDER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "cta_rail_order.json")
)

# Train Tracker route code → (dataset flag column, display name, colour)
TRAIN_LINES = {
    "Red":  ("red",  "Red Line",    "#C60C30"),
    "Blue": ("blue", "Blue Line",   "#00A1DE"),
    "Brn":  ("brn",  "Brown Line",  "#62361B"),
    "G":    ("g",    "Green Line",  "#009B3A"),
    "Org":  ("o",    "Orange Line", "#F9461C"),
    "P":    ("p",    "Purple Line", "#522398"),
    "Pink": ("pnk",  "Pink Line",   "#E27EA6"),
    "Y":    ("y",    "Yellow Line", "#F9E300"),
}
_DIR_NAMES = {"N": "Northbound", "S": "Southbound", "E": "Eastbound", "W": "Westbound"}


def _parse_local(s: str, fmt: str) -> float:
    return datetime.strptime(s, fmt).replace(tzinfo=_CHI).timestamp()


class CtaTrainProvider(TransitProvider):

    def __init__(self, agency_id: str, spec: dict):
        super().__init__(agency_id, spec)
        self._lock = threading.Lock()
        self._platforms = None       # list of dataset rows
        self._loaded_at = 0.0
        self._cache_path = os.path.join(_DATA_ROOT, agency_id, "l_stops.json")

    def _key(self) -> str:
        key = self._secret(self.spec.get("api_key_secret"))
        if not key:
            raise ProviderConfigError("CTA Train Tracker API key not set")
        return key

    def _load_platforms(self) -> list:
        with self._lock:
            if self._platforms is not None and time.time() - self._loaded_at < 30 * 86400:
                return self._platforms
            rows = None
            try:
                r = requests.get(L_STOPS_URL, timeout=20, headers=HTTP_HEADERS)
                r.raise_for_status()
                rows = r.json()
                os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
                with open(self._cache_path + ".tmp", "w") as f:
                    json.dump(rows, f)
                os.replace(self._cache_path + ".tmp", self._cache_path)
            except Exception as e:
                log.warning(f"[transit] CTA stops download failed: {e}")
                if os.path.exists(self._cache_path):
                    with open(self._cache_path) as f:
                        rows = json.load(f)
            if not rows:
                raise ProviderConfigError("Could not load CTA station list")
            self._platforms = rows
            self._loaded_at = time.time()
            return rows

    def list_routes(self) -> list[dict]:
        return [{"id": rt, "label": rt, "name": name, "color": color, "block": True}
                for rt, (_flag, name, color) in TRAIN_LINES.items()]

    def _serves(self, row: dict, route_ids: list[str]) -> bool:
        return any(row.get(TRAIN_LINES[rt][0]) in (True, "true") for rt in route_ids if rt in TRAIN_LINES)

    def list_stops(self, route_ids: list[str]) -> list[dict]:
        """Stations in line order (e.g. Red: 95th/Dan Ryan → Howard)."""
        stations = {}
        for row in self._load_platforms():
            if self._serves(row, route_ids):
                stations[row["map_id"]] = row.get("station_descriptive_name") or row.get("station_name")
        try:
            with open(RAIL_ORDER_PATH) as f:
                line_order = json.load(f)
        except Exception as e:
            log.warning(f"[transit] CTA rail order unavailable: {e}")
            line_order = {}
        ordered = [m for m in order_by_line([line_order.get(rt, []) for rt in route_ids])
                   if m in stations]
        rest = sorted((m for m in stations if m not in set(ordered)), key=lambda m: stations[m])
        return [{"id": m, "name": stations[m]} for m in ordered + rest]

    def list_directions(self, stop_id: str, route_ids: list[str]) -> list[dict]:
        bound = defaultdict(list)
        for row in self._load_platforms():
            if row.get("map_id") != stop_id or not self._serves(row, route_ids):
                continue
            d = row.get("direction_id", "")
            m = re.search(r"\(([^)]+)-bound\)", row.get("stop_name", ""))
            if m and m.group(1) not in bound[d]:
                bound[d].append(m.group(1))
            else:
                bound.setdefault(d, [])
        out = []
        for d in sorted(bound):
            label = _DIR_NAMES.get(d, d)
            if bound[d]:
                label += f" (toward {', '.join(bound[d])})"
            out.append({"key": d, "label": label})
        return out

    def get_arrivals(self, stop_id: str, route_ids: list[str],
                     direction: str = "both") -> list[Arrival]:
        r = requests.get(TRAIN_API, timeout=10, headers=HTTP_HEADERS, params={
            "key": self._key(), "mapid": stop_id, "outputType": "JSON",
        })
        r.raise_for_status()
        ctatt = r.json().get("ctatt", {})
        if str(ctatt.get("errCd", "0")) != "0":
            raise ProviderConfigError(f"CTA Train Tracker: {ctatt.get('errNm')}")

        platform_dir = {row["stop_id"]: row.get("direction_id", "")
                        for row in self._load_platforms() if row.get("map_id") == stop_id}
        wanted = set(route_ids)
        out = []
        for eta in ctatt.get("eta") or []:
            rt = eta.get("rt", "")
            if wanted and rt not in wanted:
                continue
            d = platform_dir.get(str(eta.get("stpId")), "")
            if direction not in ("", "both") and d != direction:
                continue
            _flag, _name, color = TRAIN_LINES.get(rt, ("", rt, "#C8C8C8"))
            out.append(Arrival(
                line_id=rt, line_label=rt, line_color=hex_to_rgb(color), line_block=True,
                dest=eta.get("destNm", "") or "?",
                arrival_ts=_parse_local(eta["arrT"], "%Y-%m-%dT%H:%M:%S"),
                direction_key=d,
                realtime=eta.get("isSch") != "1",
                delayed=eta.get("isDly") == "1",
                approaching=eta.get("isApp") == "1",
                trip_id=str(eta.get("rn", "")),
            ))
        out.sort(key=lambda a: a.arrival_ts)
        return out


class CtaBusProvider(TransitProvider):

    def __init__(self, agency_id: str, spec: dict):
        super().__init__(agency_id, spec)
        self._routes = None
        self._routes_at = 0.0
        self._stops: dict = {}       # route → (fetched_at, {stpid: (name, dir, lat, lon)}, {dir: [stpid…]})

    def _call(self, endpoint: str, **params) -> dict:
        key = self._secret(self.spec.get("api_key_secret"))
        if not key:
            raise ProviderConfigError("CTA Bus Tracker API key not set")
        r = requests.get(f"{BUS_API}/{endpoint}", timeout=10, headers=HTTP_HEADERS,
                         params={"key": key, "format": "json", **params})
        r.raise_for_status()
        resp = r.json().get("bustime-response", {})
        errs = resp.get("error") or []
        if errs and not any(k for k in resp if k != "error"):
            msg = "; ".join(e.get("msg", "") for e in errs)
            # "No arrival times" / "No service scheduled" just mean an empty result
            if not all(e.get("msg", "").startswith("No ") for e in errs):
                raise ProviderConfigError(f"CTA Bus Tracker: {msg}")
        return resp

    def list_routes(self) -> list[dict]:
        if self._routes is None or time.time() - self._routes_at > 86400:
            routes = self._call("getroutes").get("routes", [])
            self._routes = [{"id": r["rt"], "label": r["rt"], "name": r.get("rtnm", ""),
                             "color": r.get("rtclr", ""), "block": False} for r in routes]
            self._routes_at = time.time()
        return self._routes

    def _route_patterns(self, rt: str):
        """(stops, sequences) for a route from getpatterns — stops in travel order per direction."""
        cached = self._stops.get(rt)
        if cached and time.time() - cached[0] < 86400:
            return cached[1], cached[2]
        stops, by_dir = {}, defaultdict(list)
        for ptr in self._call("getpatterns", rt=rt).get("ptr", []):
            dname = ptr.get("rtdir", "")
            seq = []
            for pt in ptr.get("pt", []):
                if pt.get("typ") != "S" or not pt.get("stpid"):
                    continue
                sid = str(pt["stpid"])
                stops[sid] = (pt.get("stpnm", ""), dname, pt.get("lat"), pt.get("lon"))
                seq.append(sid)
            by_dir[dname].append(seq)
        seqs = {}
        for dname, pats in by_dir.items():
            merged: list = []
            for pat in sorted(pats, key=len, reverse=True):
                merge_sequence(merged, pat)
            seqs[dname] = merged
        self._stops[rt] = (time.time(), stops, seqs)
        return stops, seqs

    def _route_stops(self, rt: str) -> dict:
        return self._route_patterns(rt)[0]

    def list_stops(self, route_ids: list[str]) -> list[dict]:
        """Stops in route order; each stop's opposite-direction twin is listed next to it."""
        all_stops, sequences = {}, []
        for rt in route_ids:
            stops, seqs = self._route_patterns(rt)
            all_stops.update(stops)
            for dname in sorted(seqs):
                sequences.append(orient_line(seqs[dname], lambda s: all_stops[s][2:4]))
        ordered = order_by_line(sequences, lambda s: all_stops[s][0].lower())
        return [{"id": s, "name": f"{all_stops[s][0]}  ({all_stops[s][1]})"} for s in ordered]

    def list_directions(self, stop_id: str, route_ids: list[str]) -> list[dict]:
        dirs = []
        for rt in route_ids:
            entry = self._route_stops(rt).get(stop_id)
            if entry and entry[1] not in dirs:
                dirs.append(entry[1])
        return [{"key": d, "label": d} for d in dirs]

    def get_arrivals(self, stop_id: str, route_ids: list[str],
                     direction: str = "both") -> list[Arrival]:
        params = {"stpid": stop_id}
        if route_ids:
            params["rt"] = ",".join(route_ids[:10])
        prds = self._call("getpredictions", **params).get("prd", [])
        colors = {}
        if self._routes:
            colors = {r["id"]: r["color"] for r in self._routes}
        out = []
        for p in prds:
            if direction not in ("", "both") and p.get("rtdir") != direction:
                continue
            tm = p.get("prdtm", "")
            fmt = "%Y%m%d %H:%M:%S" if tm.count(":") == 2 else "%Y%m%d %H:%M"
            rt = p.get("rt", "")
            out.append(Arrival(
                line_id=rt, line_label=rt,
                line_color=hex_to_rgb(colors.get(rt, ""), default=(255, 255, 255)),
                line_block=False,
                dest=p.get("des", "") or "?",
                arrival_ts=_parse_local(tm, fmt),
                direction_key=p.get("rtdir", ""),
                delayed=bool(p.get("dly")),
                approaching=str(p.get("prdctdn", "")).upper() == "DUE",
                trip_id=str(p.get("vid", "")),
            ))
        out.sort(key=lambda a: a.arrival_ts)
        return out
