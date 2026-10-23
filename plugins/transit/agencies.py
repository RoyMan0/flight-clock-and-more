"""
Registry of supported transit agencies.

To add a city that publishes GTFS + GTFS-realtime, add an entry with
provider "gtfs_rt" — see providers/gtfs_rt.py for the available spec keys.
Provider instances are shared process-wide (plugin + web UI) so static
indexes and caches are loaded once.
"""

import threading

_MTA_RT = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"


def _mta_feeds() -> dict:
    groups = {
        "":      ["1", "2", "3", "4", "5", "6", "6X", "7", "7X", "GS"],
        "-ace":  ["A", "C", "E", "H", "FS"],
        "-bdfm": ["B", "D", "F", "FX", "M"],
        "-g":    ["G"],
        "-jz":   ["J", "Z"],
        "-nqrw": ["N", "Q", "R", "W"],
        "-l":    ["L"],
        "-si":   ["SI"],
    }
    return {rt: _MTA_RT + suffix for suffix, rts in groups.items() for rt in rts}


AGENCIES = {
    "cta_train": {
        "name": "Chicago — CTA 'L' Trains",
        "provider": "cta_train",
        "api_key_secret": "cta_train_api_key",
        "key_help": "Free key: transitchicago.com/developers/traintrackerapply",
        "alias_set": "cta",
    },
    "cta_bus": {
        "name": "Chicago — CTA Buses",
        "provider": "cta_bus",
        "api_key_secret": "cta_bus_api_key",
        "key_help": "Free key: ctabustracker.com → Developers",
        "alias_set": "cta",
    },
    "rtd": {
        "name": "Denver — RTD (bus & rail)",
        "provider": "gtfs_rt",
        "static_url": "https://www.rtd-denver.com/files/gtfs/google_transit.zip",
        "rt_urls": ["https://www.rtd-denver.com/files/gtfs-rt/TripUpdate.pb"],
        "trip_map": True,
        "alias_set": "rtd",
    },
    "mta_subway": {
        "name": "New York — MTA Subway",
        "provider": "gtfs_rt",
        "static_url": "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
        "rt_feeds_by_route": _mta_feeds(),
        "api_key_secret": "mta_api_key",
        "api_key_header": "x-api-key",
        "platform_dir_suffix": {"N": "0", "S": "1"},
        "direction_names": {"0": "Uptown / Northbound", "1": "Downtown / Southbound"},
        "alias_set": "mta",
    },
}

_providers: dict = {}
_lock = threading.Lock()


def get_provider(agency_id: str):
    """Return the shared provider instance for an agency, or None if unknown."""
    spec = AGENCIES.get(agency_id)
    if spec is None:
        return None
    with _lock:
        prov = _providers.get(agency_id)
        if prov is None:
            kind = spec["provider"]
            if kind == "gtfs_rt":
                from plugins.transit.providers.gtfs_rt import GtfsRtProvider as cls
            elif kind == "cta_train":
                from plugins.transit.providers.cta import CtaTrainProvider as cls
            elif kind == "cta_bus":
                from plugins.transit.providers.cta import CtaBusProvider as cls
            else:
                return None
            prov = _providers[agency_id] = cls(agency_id, spec)
        return prov


def list_agencies() -> list[dict]:
    return [
        {"id": aid, "name": s["name"], "key_secret": s.get("api_key_secret", ""),
         "key_required": s["provider"] in ("cta_train", "cta_bus"),
         "key_help": s.get("key_help", "")}
        for aid, s in AGENCIES.items()
    ]
