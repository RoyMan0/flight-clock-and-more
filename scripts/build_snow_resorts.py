"""
Generate plugins/snow_report/resorts.json — the worldwide resort list the
config page searches (name, region, country, OnTheSnow id).

Reads OnTheSnow's sitemaps for every region listing page ("/<region>/ski-resorts")
on onthesnow.com (North America) and onthesnow.co.uk (Europe), plus country pages
the .com sitemap leaves out (Japan, Australia, NZ, South America…), and collects
the resorts on each. ~600 pages at ~1/s, so it runs on a dev machine and the
result is committed. Re-run once a season or so:

    python3 scripts/build_snow_resorts.py
    python3 scripts/build_snow_resorts.py --tidy   # re-apply country names/de-dupe only
"""

import json
import os
import re
import sys
import time

import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from plugins.snow_report import ots  # noqa: E402

OUT = os.path.join(ROOT, "plugins", "snow_report", "resorts.json")
DELAY = 0.8
# Countries on onthesnow.com that its sitemap doesn't list (404s are skipped)
EXTRA_COM = ["japan", "australia", "new-zealand", "chile", "argentina", "south-korea", "china",
             "india", "iran", "lebanon", "turkey", "georgia", "russia", "kazakhstan", "south-africa"]


# OnTheSnow's internal country codes → names people search for
COUNTRIES = {
    "USA": "United States", "CAN": "Canada", "AT": "Austria", "FR": "France", "IT": "Italy",
    "CH": "Switzerland", "DE": "Germany", "SK": "Slovakia", "CZ": "Czechia", "NO": "Norway",
    "PL": "Poland", "SE": "Sweden", "ES": "Spain", "RO": "Romania", "NZ": "New Zealand",
    "SI": "Slovenia", "GE": "Georgia", "AU": "Australia", "BE": "Belgium", "FI": "Finland",
    "SCT": "Scotland", "CL": "Chile", "AJPCNTR": "Japan", "ARG": "Argentina", "AD": "Andorra",
    "BG": "Bulgaria", "RU": "Russia", "BA": "Bosnia and Herzegovina", "LI": "Liechtenstein",
    "PT": "Portugal", "GB": "United Kingdom", "UK": "United Kingdom", "AND": "Andorra",
}


def tidy(resorts: list) -> list:
    """Readable country names; one entry per resort (logical regions list some twice)."""
    out, seen = [], set()
    for r in resorts:
        code = r.get("country", "")
        r["country"] = COUNTRIES.get(code, COUNTRIES.get(code.split("-")[0], code))
        key = (r["name"].lower(), r["country"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return sorted(out, key=lambda r: (r["country"], r["region"], r["name"]))


def sitemap_regions(edition: str) -> list:
    r = requests.get(f"{ots.EDITIONS[edition]}/sitemap_website.xml", headers=ots.HEADERS, timeout=60)
    r.raise_for_status()
    return re.findall(r"<loc>https://[^/]+/([a-z0-9-]+)/ski-resorts</loc>", r.text)


def main():
    if "--tidy" in sys.argv:
        with open(OUT) as f:
            out = tidy(json.load(f))
        with open(OUT, "w") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Tidied {len(out)} resorts")
        return
    jobs = [("com", reg) for reg in sitemap_regions("com") + EXTRA_COM]
    jobs += [("uk", reg) for reg in sitemap_regions("uk")]
    seen_jobs, resorts = set(), {}
    for n, (edition, region) in enumerate(jobs, 1):
        if (edition, region) in seen_jobs:
            continue
        seen_jobs.add((edition, region))
        try:
            found = ots.list_region(edition, region)
        except ots.SnowError as e:
            print(f"[{n}/{len(jobs)}] {edition}/{region}: {e}")
            found = []
        for r in found:
            resorts.setdefault(r["id"], r)
        print(f"[{n}/{len(jobs)}] {edition}/{region}: {len(found)} (total {len(resorts)})", flush=True)
        time.sleep(DELAY)
    out = tidy(list(resorts.values()))
    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(out)} resorts to {OUT}")


if __name__ == "__main__":
    main()
