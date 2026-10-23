"""
Common types for transit providers.

Every provider normalises its agency's data into `Arrival` objects so the
plugin and renderer never need to know which API the data came from.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

HTTP_HEADERS = {"User-Agent": "LEDMatrix/1.0"}


class ProviderNotReady(Exception):
    """Raised while a provider is still preparing data (e.g. building a GTFS index)."""


class ProviderConfigError(Exception):
    """Raised when a provider can't run because of missing setup (e.g. no API key)."""


@dataclass
class Arrival:
    line_id: str
    line_label: str             # short text label, e.g. "A", "22", "Red"
    line_color: tuple           # RGB
    line_block: bool            # True → draw a solid colour block instead of the label
    dest: str                   # full destination name (abbreviated at render time)
    arrival_ts: float           # UTC epoch seconds
    direction_key: str = ""
    realtime: bool = True
    delayed: bool = False
    approaching: bool = False   # provider says "due"/"approaching" regardless of minutes
    trip_id: str = field(default="", compare=False)
    alias_set: str = field(default="", compare=False)
    missable: bool = field(default=False, compare=False)  # arrives before the walk time allows   # set by the plugin: which built-in aliases apply


def hex_to_rgb(hex_color: str, default=(200, 200, 200)) -> tuple:
    h = (hex_color or "").strip().lstrip("#")
    if len(h) != 6:
        return default
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


def merge_sequence(result: list, seq: list, name_of=None) -> list:
    """
    Merge an ordered stop sequence into `result` in place, keeping line order.
    Stops already present act as anchors; new stops go right after the previous
    anchor — or, when `name_of` is given, right after a same-named stop (so a
    bus stop's opposite-direction twin lands next to it).
    """
    pos = {s: i for i, s in enumerate(result)}
    names = {}
    if name_of:
        for i, s in enumerate(result):
            names.setdefault(name_of(s), i)
    anchor = -1
    for s in seq:
        if s in pos:
            # Only move forward — a repeated stop (loop, or several gates of one
            # station) must not pull later stops back up the list
            anchor = max(anchor, pos[s])
            continue
        twin = names.get(name_of(s)) if name_of else None
        at = (twin if twin is not None else anchor) + 1
        result.insert(at, s)
        pos = {x: i for i, x in enumerate(result)}
        if name_of:
            names = {}
            for i, x in enumerate(result):
                names.setdefault(name_of(x), i)
        anchor = at
    return result


def orient_line(seq: list, coord_of) -> list:
    """
    Put a line in a consistent geographic order: south → north for lines that
    mostly run north-south (CTA Red: 95th → Howard), west → east for lines
    that mostly run east-west (a crosstown bus).
    """
    if len(seq) > 1:
        a, b = coord_of(seq[0]), coord_of(seq[-1])
        if a and b and None not in a and None not in b:
            dlat, dlon = b[0] - a[0], b[1] - a[1]
            if (dlat < 0) if abs(dlat) >= abs(dlon) else (dlon < 0):
                return list(reversed(seq))
    return list(seq)


def order_by_line(sequences: list, name_of=None) -> list:
    """Combine already-oriented sequences (route by route) into one display order."""
    result: list = []
    for seq in sequences:
        merge_sequence(result, seq, name_of)
    return result


class TransitProvider:
    """Interface every agency adapter implements."""

    def __init__(self, agency_id: str, spec: dict):
        self.agency_id = agency_id
        self.spec = spec

    # -- setup / UI ----------------------------------------------------
    def status(self) -> dict:
        """{"ready": bool, "message": str} — used by the web UI."""
        return {"ready": True, "message": ""}

    def list_routes(self) -> list[dict]:
        """[{id, label, name, color: '#rrggbb', block: bool}]"""
        raise NotImplementedError

    def list_stops(self, route_ids: list[str]) -> list[dict]:
        """[{id, name}] — stops served by any of the given routes."""
        raise NotImplementedError

    def list_directions(self, stop_id: str, route_ids: list[str]) -> list[dict]:
        """[{key, label}] — directions of travel available at this stop."""
        raise NotImplementedError

    # -- live data -----------------------------------------------------
    def get_arrivals(self, stop_id: str, route_ids: list[str],
                     direction: str = "both") -> list[Arrival]:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    def _secret(self, name: Optional[str]) -> str:
        if not name:
            return ""
        try:
            from core.config_manager import get_config
            return (get_config().get_secret(name, "") or "").strip()
        except Exception:
            return ""
