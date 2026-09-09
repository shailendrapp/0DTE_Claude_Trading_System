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

FAIL-SAFE: if fetching/parsing fails for a year, this prints a clear
error and leaves that year OUT of the written cache rather than writing
partial/wrong data -- any year missing from the cache falls back to
econ_calendar.py's hardcoded lists (if that year is 2026) or to an empty
list (any other year), which only means event-risk gating is unavailable
for that year, not wrong. Exits non-zero if EVERY year failed, so the
scheduled workflow shows red and someone notices -- a single year's
failure (e.g. next year's BLS schedule not published yet) exits 0 since
that's an expected, not exceptional, state.
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
        try:
            result = fetch_all(year)
            cache[str(year)] = {k: [d.isoformat() for d in v] for k, v in result.items()}
            print(f"[ok] {year}: FOMC={len(result['fomc'])} CPI={len(result['cpi'])} "
                  f"PPI={len(result['ppi'])} NFP={len(result['nfp'])}")
            any_success = True
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {year}: {e}")

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
