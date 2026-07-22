#!/usr/bin/env python3
"""
Remove pre-logging-fix flight records where airline and aircraft type were
never captured (both fields empty). Flights from 2026-05-13 onward are left
untouched — empty fields there mean the flight was genuinely unidentifiable.

Usage:
    python3 scripts/clean_history_unknowns.py          # dry-run (default)
    python3 scripts/clean_history_unknowns.py --commit  # write changes
"""

import json
import os
import sys
from datetime import date

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "flight_history.json")

# Airline + aircraft type logging was added in commit 41aba9f on this date.
# Records strictly before this date with both fields empty are safe to remove.
CUTOFF = date(2026, 5, 13)


def main():
    commit = "--commit" in sys.argv

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    removed_flights = 0
    removed_days = 0
    kept_unknowns = 0
    modified_days = []

    for date_str in sorted(history.keys()):
        try:
            day_date = date.fromisoformat(date_str)
        except ValueError:
            continue

        if day_date >= CUTOFF:
            # Count how many unknowns remain in post-cutoff data (informational)
            for fl in history[date_str].get("flights", []):
                if not (fl.get("airline_icao") or "").strip() and not (fl.get("aircraft_type") or "").strip():
                    kept_unknowns += 1
            continue

        day = history[date_str]
        flights = day.get("flights", [])
        before = len(flights)
        kept = [
            fl for fl in flights
            if (fl.get("airline_icao") or "").strip() or (fl.get("aircraft_type") or "").strip()
        ]
        dropped = before - len(kept)

        if dropped == 0:
            continue

        removed_flights += dropped
        modified_days.append((date_str, before, len(kept)))

        if commit:
            if kept:
                day["flights"] = kept
                day["count"] = len(kept)
            else:
                del history[date_str]
                removed_days += 1

    # Summary
    print(f"{'DRY RUN — ' if not commit else ''}Cutoff: before {CUTOFF}")
    print()
    if modified_days:
        print(f"{'Would remove' if not commit else 'Removed'} {removed_flights} old unknown flights across {len(modified_days)} date(s):")
        for date_str, before, after in modified_days:
            if after == 0:
                print(f"  {date_str}: {before} flights → day entry deleted")
            else:
                print(f"  {date_str}: {before} flights → {after} kept ({before - after} removed)")
    else:
        print("No pre-cutoff unknown flights found.")

    print()
    print(f"Post-cutoff unknown flights kept: {kept_unknowns}")

    if not commit:
        print()
        print("Run with --commit to apply changes.")
    else:
        if removed_flights > 0:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            print(f"\nSaved {HISTORY_FILE}")


if __name__ == "__main__":
    main()
