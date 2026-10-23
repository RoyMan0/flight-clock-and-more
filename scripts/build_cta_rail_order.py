"""
Generate plugins/transit/cta_rail_order.json — the station order of each
CTA 'L' line, used to list stations in line order in the config page.

CTA's GTFS is ~70 MB zipped (370 MB of stop_times), too heavy to process on a
Pi, so this runs on a dev machine and the small result is committed. Re-run
only when stations open/close:

    curl -L -o /tmp/cta.zip https://www.transitchicago.com/downloads/sch_data/google_transit.zip
    python3 scripts/build_cta_rail_order.py /tmp/cta.zip

Output: {"Red": ["40450", ...], ...} — station map_ids (Train Tracker "mapid"),
south → north (CTA Red: 95th/Dan Ryan → Howard).
"""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from plugins.transit.providers.base import order_by_line, orient_line  # noqa: E402
from plugins.transit.providers.cta import TRAIN_LINES                  # noqa: E402
from plugins.transit.providers.gtfs_rt import GtfsRtProvider           # noqa: E402

OUT = os.path.join(ROOT, "plugins", "transit", "cta_rail_order.json")


def main(zip_path: str):
    prov = GtfsRtProvider("cta_rail_build", {"static_url": ""})
    idx = prov._parse_static(zip_path)
    stops = idx["stops"]

    def station(sid):
        return stops.get(sid, ["", ""])[1] or sid

    def coord(sid):
        s = stops.get(sid)
        return (s[2], s[3]) if s else None

    out = {}
    for rt in TRAIN_LINES:
        seqs = []
        for _d, seq in sorted(idx["route_seq"].get(rt, {}).items()):
            st_seq = []
            for sid in seq:
                st = station(sid)
                if st not in st_seq:
                    st_seq.append(st)
            seqs.append(orient_line(st_seq, coord))
        out[rt] = order_by_line(seqs)
        names = [stops.get(s, [s])[0] for s in out[rt]]
        print(f"{rt:5} {len(names):3} stations: {names[0]} → {names[-1]}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
