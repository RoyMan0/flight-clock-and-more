#!/usr/bin/env python3
"""
Generate a 64×32 pixel land/ocean mask PNG for the world_daylight plugin.

Uses the Natural Earth 110m simplified land polygons (GeoJSON) from GitHub.
Requires: Pillow, requests

Run once (e.g. on the Pi) and commit the output:
  python3 scripts/generate_land_mask.py
"""

import json
import math
import os
import struct
import sys
import zlib

MATRIX_W = 64
MATRIX_H = 32
OUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "world_daylight", "land_mask.png"
)
GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_110m_land.geojson"
)


def lon_to_x(lon: float) -> int:
    return int((lon + 180.0) * MATRIX_W / 360.0) % MATRIX_W


def lat_to_y(lat: float) -> int:
    return int((90.0 - lat) * MATRIX_H / 180.0)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def rasterize_polygon(pixels: list, ring: list):
    """Scanline fill a polygon ring into a MATRIX_H×MATRIX_W bool grid."""
    if len(ring) < 3:
        return

    coords = [(lon_to_x(c[0]), lat_to_y(c[1])) for c in ring]

    min_y = clamp(min(c[1] for c in coords), 0, MATRIX_H - 1)
    max_y = clamp(max(c[1] for c in coords), 0, MATRIX_H - 1)

    for y in range(min_y, max_y + 1):
        intersections = []
        n = len(coords)
        for i in range(n):
            x0, y0 = coords[i]
            x1, y1 = coords[(i + 1) % n]
            if y0 == y1:
                continue
            if min(y0, y1) <= y < max(y0, y1):
                t = (y - y0) / (y1 - y0)
                xi = x0 + t * (x1 - x0)
                intersections.append(xi)
        intersections.sort()
        for k in range(0, len(intersections) - 1, 2):
            x_lo = clamp(int(math.ceil(intersections[k])), 0, MATRIX_W - 1)
            x_hi = clamp(int(intersections[k + 1]), 0, MATRIX_W - 1)
            for x in range(x_lo, x_hi + 1):
                pixels[y][x] = 1


def write_png(path: str, pixels: list):
    """Write a 1-bit-per-pixel greyscale PNG without external libraries."""
    width = len(pixels[0])
    height = len(pixels)

    def make_chunk(tag: bytes, data: bytes) -> bytes:
        length = len(data)
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", length) + tag + data + struct.pack(">I", crc)

    # IHDR: 8-bit greyscale (type 0) — write as 8-bit grey for broad PIL compat
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    ihdr = make_chunk(b"IHDR", ihdr_data)

    # IDAT: raw image data
    raw_rows = []
    for row in pixels:
        raw_rows.append(b"\x00" + bytes(255 if v else 0 for v in row))
    compressed = zlib.compress(b"".join(raw_rows), 9)
    idat = make_chunk(b"IDAT", compressed)

    iend = make_chunk(b"IEND", b"")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(ihdr)
        f.write(idat)
        f.write(iend)


def fetch_geojson_urllib() -> dict:
    import urllib.request
    print(f"Downloading Natural Earth 110m land data from GitHub…")
    with urllib.request.urlopen(GEOJSON_URL, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_geojson_requests() -> dict:
    import requests  # type: ignore
    print(f"Downloading Natural Earth 110m land data from GitHub…")
    resp = requests.get(GEOJSON_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main():
    # Try to get GeoJSON — prefer requests for better error messages
    try:
        data = fetch_geojson_requests()
    except ImportError:
        data = fetch_geojson_urllib()

    pixels = [[0] * MATRIX_W for _ in range(MATRIX_H)]
    land_count = 0

    for feature in data.get("features", []):
        geom = feature.get("geometry", {})
        geo_type = geom.get("type", "")
        coords = geom.get("coordinates", [])

        if geo_type == "Polygon":
            polys = [coords[0]] if coords else []
        elif geo_type == "MultiPolygon":
            polys = [p[0] for p in coords if p]
        else:
            continue

        for ring in polys:
            rasterize_polygon(pixels, ring)
            land_count += 1

    # Count land pixels
    total_land = sum(pixels[y][x] for y in range(MATRIX_H) for x in range(MATRIX_W))
    print(f"Rasterized {land_count} polygons → {total_land} land pixels "
          f"({100*total_land//(MATRIX_W*MATRIX_H)}% of {MATRIX_W}×{MATRIX_H})")

    write_png(OUT_PATH, pixels)
    print(f"Saved: {OUT_PATH}")


if __name__ == "__main__":
    main()
