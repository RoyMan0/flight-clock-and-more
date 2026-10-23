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


def hex_to_rgb(hex_color: str, default=(200, 200, 200)) -> tuple:
    h = (hex_color or "").strip().lstrip("#")
    if len(h) != 6:
        return default
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default


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
