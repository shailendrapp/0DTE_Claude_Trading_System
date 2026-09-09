#!/usr/bin/env python3
"""
CLI: refresh data/econ_calendar_cache.json from the Fed's/BLS's published
schedules (see modules/econ_calendar_fetch.py). Meant to run on a slow
cadence (weekly is plenty -- these schedules are published months/a year
in advance and essentially never change) via .github/workflows/
calendar_refresh.yml, NOT on every trading decision -- keeping the live
scrape decoupled from run_live.py means a scraper break can never delay
or corrupt an actual trading decision; worst case run_live.py just keeps
using whatever was last successfully cached (or the hardcoded fallback
lists in econ_calendar.py, if the cache has never been built yet).

Usage:
    python refresh_econ_calendar.py                  # this year + next year
    python refresh_econ_calendar.py --years 2026 2027

FAIL-SAFE, per SERIES not per year: fetch_all() now returns whichever of
FOMC/CPI/PPI/NFP succeeded for a year even if others failed (e.g. FOMC
scraping works while BLS blocks CPI/PPI/NFP with a 403) -- a failed
series for a year keeps whatever was already cached for it (or falls
back to econ_calendar.py's hardcoded 2026 lists / an empty list if
nothing was ever cached), never gets overwritten with nothing. Exits
non-zero only if EVERY series for EVERY requested year failed, so the
scheduled workflow shows red and someone notices -- a single series/
year failure (e.g. BLS still blocked, or next year's schedule not
published yet) exits 0 since that's an expected, not exceptional, state.

BUG HISTORY (2026-09-09): earlier versions treated a year as all-or-
nothing -- one failing series (BLS) meant a working series (FOMC, once
its parser got fixed) never got written either. Fixed by merging at
the series level.
"""
import argparse
import datetime as dt
import json
import os

from modules.econ_calendar_fetch import fetch_all

CACHE_PATH = os.path.join("data", "econ_calendar_cache.json")


def main():
    ap = argparse.ArgumentParser()
    this_year = dt.date.today().year
    ap.add_argument("--years", type=int, nargs="+", default=[this_year, this_year + 1])
    ap.add_argument("--out", default=CACHE_PATH)
    args = ap.parse_args()

    existing = {}
    if os.path.exists(args.out):
        with open(args.out) as f:
            existing = json.load(f)

    cache = existing.get("years", {})
    any_success = False
    for year in args.years:
        out = fetch_all(year)
        succeeded, failed = out["series"], out["errors"]
        if succeeded:
            year_cache = dict(cache.get(str(year), {}))  # keep any series already cached for this year
            for name, dates in succeeded.items():
                year_cache[name] = [d.isoformat() for d in dates]
            cache[str(year)] = year_cache
            any_success = True
            print(f"[ok] {year}: " + ", ".join(f"{name}={len(dates)}" for name, dates in succeeded.items()))
        for name, err in failed.items():
            print(f"[skip] {year}.{name}: {err}")

    if not any_success and not existing:
        raise SystemExit("Every requested year failed to fetch and there's no existing cache -- "
                          "not writing anything. Check network access and the page structure.")
    if not any_success:
        raise SystemExit("Every requested year failed to fetch this run -- leaving the existing "
                          "cache untouched (not overwriting good data with nothing).")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"generated_at": dt.datetime.utcnow().isoformat() + "Z", "years": cache}, f, indent=2)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
