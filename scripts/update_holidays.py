#!/usr/bin/env python3
"""Fetch Malaysia public holidays from Calendarific API and update bundled JSON.

Usage:
    python scripts/update_holidays.py

Requires CALENDARIFIC_API_KEY env var (get one free at calendarific.com).
Fetches current year and next year. Preserves manually added dates
from years beyond the fetched range.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

API_URL = "https://calendarific.com/api/v2/holidays"
HOLIDAYS_FILE = Path(__file__).resolve().parent.parent / "crow_agent" / "holidays_my.json"

# Holiday types that represent actual days off in Malaysia
_HOLIDAY_TYPES = frozenset({"National holiday", "Common local holiday", "Local holiday"})


def fetch_year(api_key: str, year: int) -> list[str]:
    resp = requests.get(
        API_URL,
        params={"api_key": api_key, "country": "MY", "year": year},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("meta", {}).get("code") != 200:
        raise RuntimeError(f"API error: {data}")
    holidays = data.get("response", {}).get("holidays", [])
    return [
        h["date"]["iso"]
        for h in holidays
        if any(t in _HOLIDAY_TYPES for t in h.get("type", []))
    ]


def main() -> int:
    api_key = os.environ.get("CALENDARIFIC_API_KEY", "").strip()
    if not api_key:
        print("CALENDARIFIC_API_KEY not set. Skipping fetch.", file=sys.stderr)
        return 1

    current_year = datetime.now().year
    years_to_fetch = [current_year, current_year + 1]

    # Load existing
    existing: set[str] = set()
    if HOLIDAYS_FILE.exists():
        existing = set(json.loads(HOLIDAYS_FILE.read_text()).get("holidays", []))

    # Fetch new data
    fetched: set[str] = set()
    for year in years_to_fetch:
        try:
            dates = fetch_year(api_key, year)
            fetched.update(dates)
            print(f"  {year}: {len(dates)} holidays")
        except requests.RequestException as e:
            print(f"  {year}: FAILED — {e}", file=sys.stderr)
        except RuntimeError as e:
            print(f"  {year}: {e}", file=sys.stderr)

    if not fetched:
        print("No data fetched. File unchanged.", file=sys.stderr)
        return 1

    # Merge: preserve manually added dates from years NOT covered by API fetch
    fetched_years = set(str(y) for y in years_to_fetch)
    manual = {d for d in existing if d.split("-")[0] not in fetched_years}
    merged = sorted(fetched | manual)

    HOLIDAYS_FILE.write_text(
        json.dumps(
            {
                "description": "Malaysia public holidays. Auto-updated by scripts/update_holidays.py.",
                "format": "YYYY-MM-DD",
                "holidays": merged,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Wrote {len(merged)} holidays ({len(fetched)} from API + {len(manual)} manual) to {HOLIDAYS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
