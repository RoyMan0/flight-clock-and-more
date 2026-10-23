"""
Generic GTFS + GTFS-realtime provider.

Static GTFS (routes, stops, trips, stop_times) is downloaded once and boiled
down to a compact index in data/transit/<agency>/index.json, so only small
lookup tables live in memory (the Pi 3 can't hold full stop_times).
Realtime arrivals come from the agency's TripUpdates protobuf feed(s).

Registry spec keys (see agencies.py):
  static_url            GTFS zip URL
  rt_urls               list of TripUpdates feed URLs (fetched every update), or
  rt_feeds_by_route     {route_id: url} — fetch only the feeds for selected routes
  api_key_secret        optional secrets.json key; sent as `api_key_header`
  api_key_header        header name for the key (default "x-api-key")
  trip_map              True when realtime trip_ids match static trip_ids
                        (lets us recover direction + headsign per trip)
  platform_dir_suffix   {"N": "0", "S": "1"} — infer direction from platform stop_id suffix
  direction_names       {"0": "Uptown", "1": "Downtown"} — friendly direction labels
  refresh_days          rebuild the static index after this many days (default 7)
"""

import csv
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from collections import Counter, defaultdict

import requests

from plugins.transit.providers.base import (
    HTTP_HEADERS, Arrival, ProviderConfigError, ProviderNotReady,
    TransitProvider, hex_to_rgb,
)

log = logging.getLogger(__name__)

_DATA_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "transit")
)
_INDEX_VERSION = 1
_FEED_CACHE_SECS = 15


def _natural_key(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s or "")]


class GtfsRtProvider(TransitProvider):

    def __init__(self, agency_id: str, spec: dict):
        super().__init__(agency_id, spec)
        self._dir = os.path.join(_DATA_ROOT, agency_id)
        self._index_path = os.path.join(self._dir, "index.json")
        self._lock = threading.Lock()
        self._index = None
        self._children: dict = {}
        self._building = False
        self._build_error = ""
        self._feed_cache: dict = {}   # url → (fetched_at, FeedMessage)

    # ------------------------------------------------------------------
    # Static index
    # ------------------------------------------------------------------

    def _ensure_index(self) -> dict:
        with self._lock:
            if self._index is None and os.path.exists(self._index_path):
                try:
                    with open(self._index_path) as f:
                        idx = json.load(f)
                    if idx.get("version") == _INDEX_VERSION:
                        self._set_index(idx)
                except Exception as e:
                    log.warning(f"[transit] {self.agency_id}: bad index file, rebuilding: {e}")

            refresh = float(self.spec.get("refresh_days", 7)) * 86400
            stale = self._index is not None and time.time() - self._index.get("built", 0) > refresh
            if (self._index is None or stale) and not self._building:
                self._building = True
                self._build_error = ""
                threading.Thread(target=self._build_index, daemon=True,
                                 name=f"transit-gtfs-{self.agency_id}").start()

            if self._index is None:
                raise ProviderNotReady(self._build_error or "Downloading schedule data…")
            return self._index

    def _set_index(self, idx: dict):
        children = defaultdict(list)
        for sid, (_name, parent) in idx["stops"].items():
            if parent:
                children[parent].append(sid)
        self._index = idx
        self._children = dict(children)

    def _build_index(self):
        t0 = time.time()
        os.makedirs(self._dir, exist_ok=True)
        zpath = os.path.join(self._dir, "static.zip")
        try:
            log.info(f"[transit] {self.agency_id}: downloading static GTFS")
            with requests.get(self.spec["static_url"], stream=True, timeout=60,
                              headers=HTTP_HEADERS) as r:
                r.raise_for_status()
                with open(zpath + ".tmp", "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            os.replace(zpath + ".tmp", zpath)

            idx = self._parse_static(zpath)
            tmp = self._index_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(idx, f, separators=(",", ":"))
            os.replace(tmp, self._index_path)
            with self._lock:
                self._set_index(idx)
            log.info(f"[transit] {self.agency_id}: index built in {time.time() - t0:.0f}s "
                     f"({len(idx['routes'])} routes, {len(idx['stops'])} stops)")
        except Exception as e:
            self._build_error = f"Schedule download failed: {e}"
            log.warning(f"[transit] {self.agency_id}: index build failed: {e}")
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
            self._building = False

    def _parse_static(self, zpath: str) -> dict:
        keep_trips = bool(self.spec.get("trip_map"))

        def reader(z, name):
            f = io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline="")
            rd = csv.reader(f)
            header = next(rd)
            return {h.strip(): i for i, h in enumerate(header)}, rd

        with zipfile.ZipFile(zpath) as z:
            routes = {}
            col, rd = reader(z, "routes.txt")
            for row in rd:
                rid = row[col["route_id"]]
                short = row[col["route_short_name"]].strip() if "route_short_name" in col else ""
                long_ = row[col["route_long_name"]].strip() if "route_long_name" in col else ""
                color = row[col["route_color"]].strip() if "route_color" in col else ""
                routes[rid] = {
                    "label": short or rid,
                    "name": long_,
                    "color": f"#{color}" if color else "",
                    "type": row[col["route_type"]] if "route_type" in col else "",
                }

            stops = {}
            col, rd = reader(z, "stops.txt")
            has_parent = "parent_station" in col
            for row in rd:
                stops[row[col["stop_id"]]] = [
                    row[col["stop_name"]].strip(),
                    row[col["parent_station"]].strip() if has_parent else "",
                ]

            trips = {}                      # trip_id → (route, dir, headsign)
            headsign_counts = defaultdict(Counter)
            col, rd = reader(z, "trips.txt")
            has_dir = "direction_id" in col
            has_hs = "trip_headsign" in col
            for row in rd:
                route = row[col["route_id"]]
                d = row[col["direction_id"]].strip() if has_dir else ""
                hs = row[col["trip_headsign"]].strip() if has_hs else ""
                trips[row[col["trip_id"]]] = (route, d, hs)
                if hs:
                    headsign_counts[f"{route}|{d}"][hs] += 1

            stop_routes = defaultdict(set)  # stop_id → {"route|dir"}
            col, rd = reader(z, "stop_times.txt")
            ti, si = col["trip_id"], col["stop_id"]
            for row in rd:
                t = trips.get(row[ti])
                if t:
                    stop_routes[row[si]].add(f"{t[0]}|{t[1]}")

        return {
            "version": _INDEX_VERSION,
            "built": time.time(),
            "routes": routes,
            "stops": stops,
            "stop_routes": {k: sorted(v) for k, v in stop_routes.items()},
            "headsigns": {k: c.most_common(1)[0][0] for k, c in headsign_counts.items()},
            "trips": {tid: [d, hs] for tid, (_r, d, hs) in trips.items()} if keep_trips else {},
        }

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def status(self) -> dict:
        try:
            self._ensure_index()
            return {"ready": True, "message": ""}
        except ProviderNotReady as e:
            return {"ready": False, "message": str(e), "preparing": self._building}

    def list_routes(self) -> list[dict]:
        idx = self._ensure_index()
        served = {k.split("|", 1)[0] for keys in idx["stop_routes"].values() for k in keys}
        out = [
            {"id": rid, "label": r["label"], "name": r["name"], "color": r["color"], "block": False}
            for rid, r in idx["routes"].items() if rid in served
        ]
        out.sort(key=lambda r: _natural_key(r["label"]))
        return out

    def _station_of(self, sid: str) -> str:
        parent = self._index["stops"].get(sid, ["", ""])[1]
        return parent or sid

    def _dir_label(self, d: str, route_ids: list[str]) -> str:
        hs = []
        for rid in route_ids:
            h = self._index["headsigns"].get(f"{rid}|{d}")
            if h and h not in hs:
                hs.append(h)
        toward = ("Toward " + " / ".join(hs[:3])) if hs else ""
        name = (self.spec.get("direction_names") or {}).get(d)
        if name:
            return f"{name} ({toward.lower()[:1] + toward[1:]})" if toward else name
        return toward or f"Direction {d}"

    def list_stops(self, route_ids: list[str]) -> list[dict]:
        idx = self._ensure_index()
        wanted = set(route_ids)
        station_dirs = defaultdict(set)
        for sid, keys in idx["stop_routes"].items():
            for k in keys:
                rid, d = k.split("|", 1)
                if rid in wanted:
                    station_dirs[self._station_of(sid)].add(d)

        out = []
        for st, dirs in station_dirs.items():
            name = idx["stops"].get(st, [st])[0]
            # A one-directional stop (most bus stops) gets its heading in the label
            if len(dirs) == 1:
                d = next(iter(dirs))
                name = f"{name}  → {self._dir_label(d, route_ids).replace('Toward ', '')}"
            out.append({"id": st, "name": name})
        out.sort(key=lambda s: _natural_key(s["name"]))
        return out

    def _target_stops(self, stop_id: str) -> set:
        targets = {stop_id}
        for child in self._children.get(stop_id, []):
            targets.add(child)
            targets.update(self._children.get(child, []))
        return targets

    def list_directions(self, stop_id: str, route_ids: list[str]) -> list[dict]:
        idx = self._ensure_index()
        wanted = set(route_ids)
        dirs = set()
        for sid in self._target_stops(stop_id):
            for k in idx["stop_routes"].get(sid, []):
                rid, d = k.split("|", 1)
                if not wanted or rid in wanted:
                    dirs.add(d)
        return [{"key": d, "label": self._dir_label(d, route_ids)} for d in sorted(dirs)]

    # ------------------------------------------------------------------
    # Realtime
    # ------------------------------------------------------------------

    def _feed_urls(self, route_ids: list[str]) -> list[str]:
        if self.spec.get("rt_urls"):
            return list(self.spec["rt_urls"])
        by_route = self.spec.get("rt_feeds_by_route") or {}
        urls = []
        for rid in route_ids or by_route.keys():
            u = by_route.get(rid)
            if u and u not in urls:
                urls.append(u)
        return urls

    def _fetch_feed(self, url: str):
        try:
            from google.transit import gtfs_realtime_pb2
        except ImportError:
            raise ProviderConfigError("gtfs-realtime-bindings is not installed "
                                      "(pip install gtfs-realtime-bindings)")
        now = time.time()
        cached = self._feed_cache.get(url)
        if cached and now - cached[0] < _FEED_CACHE_SECS:
            return cached[1]
        headers = dict(HTTP_HEADERS)
        key = self._secret(self.spec.get("api_key_secret"))
        if key:
            headers[self.spec.get("api_key_header", "x-api-key")] = key
        r = requests.get(url, timeout=15, headers=headers)
        r.raise_for_status()
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(r.content)
        self._feed_cache[url] = (now, msg)
        return msg

    def get_arrivals(self, stop_id: str, route_ids: list[str],
                     direction: str = "both") -> list[Arrival]:
        idx = self._ensure_index()
        targets = self._target_stops(stop_id)
        wanted = set(route_ids)
        trips = idx["trips"]
        suffix_dirs = self.spec.get("platform_dir_suffix") or {}
        station_name = idx["stops"].get(stop_id, [""])[0]
        now = time.time()
        out = []

        for url in self._feed_urls(route_ids):
            try:
                feed = self._fetch_feed(url)
            except ProviderConfigError:
                raise
            except Exception as e:
                log.warning(f"[transit] {self.agency_id}: feed fetch failed ({url}): {e}")
                continue

            for ent in feed.entity:
                if not ent.HasField("trip_update"):
                    continue
                tu = ent.trip_update
                rid = tu.trip.route_id
                if wanted and rid not in wanted:
                    continue
                stus = tu.stop_time_update
                n = len(stus)
                for i, stu in enumerate(stus):
                    if stu.stop_id not in targets:
                        continue
                    ts = (stu.arrival.time if stu.HasField("arrival") else 0) or \
                         (stu.departure.time if stu.HasField("departure") else 0)
                    if not ts or ts < now - 30:
                        break
                    # Train/bus terminating here — nobody can board it
                    if i == n - 1 and n > 1:
                        break

                    tinfo = trips.get(tu.trip.trip_id)
                    if tu.trip.HasField("direction_id"):
                        d = str(tu.trip.direction_id)
                    elif tinfo:
                        d = tinfo[0]
                    else:
                        d = suffix_dirs.get(stu.stop_id[-1:], "")
                    if direction not in ("", "both") and d != direction:
                        break

                    dest = tinfo[1] if tinfo and tinfo[1] else ""
                    if not dest and n:
                        last = self._station_of(stus[n - 1].stop_id)
                        dest = idx["stops"].get(last, [""])[0]
                    if not dest:
                        dest = idx["headsigns"].get(f"{rid}|{d}", "")
                    if dest and dest == station_name:
                        break

                    r = idx["routes"].get(rid, {})
                    out.append(Arrival(
                        line_id=rid,
                        line_label=r.get("label", rid),
                        line_color=hex_to_rgb(r.get("color", "")),
                        line_block=False,
                        dest=dest or "?",
                        arrival_ts=float(ts),
                        direction_key=d,
                        trip_id=tu.trip.trip_id,
                    ))
                    break

        out.sort(key=lambda a: a.arrival_ts)
        return out
