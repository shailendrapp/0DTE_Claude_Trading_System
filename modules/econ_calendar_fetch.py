"""
Live scraper for FOMC/CPI/PPI/NFP release dates, so the hardcoded
FOMC_DATES_2026 etc. lists in econ_calendar.py don't need manual
maintenance every year (and so 2027+ dates exist at all once published).

Sources (both publish full-year schedules far in advance, no paid
calendar needed):
  - Federal Reserve: federalreserve.gov/monetarypolicy/fomccalendars.htm
  - BLS: bls.gov/schedule/<year>/home.htm

HONESTY UP FRONT: this environment (the one this code was written in) has
no general internet egress -- fetching these pages here always fails with
a connection error, so this parser was built by inspecting page CONTENT
retrieved through a separate tool, not by testing this exact requests/
BeautifulSoup code against the live page. It should work once it runs
somewhere with normal internet access (a GitHub Actions runner does), but
the parsing regexes below are a best-effort match to the page structure
as observed on 2026-09-09, not a guarantee it still matches whenever you
read this. TEST IT: run refresh_econ_calendar.py manually once (see that
file) and sanity-check the dates it finds against the Fed's/BLS's site
yourself before trusting it unattended.

FAIL-SAFE DESIGN: every function here either returns a real, validated
list of dates, or raises -- callers (refresh_econ_calendar.py) must catch
failures and leave the existing cache/hardcoded list untouched rather
than write partial or wrong data. A broken scraper should degrade to
"stale but correct" data, never to silently wrong event-risk gating.
"""
from __future__ import annotations
import re
from datetime import date

try:
    import requests
except ImportError:
    requests = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
MONTH_RE = "|".join(MONTHS.keys())

FULL_DATE_RE = re.compile(rf"({MONTH_RE})\s+(\d{{1,2}}),?\s+(\d{{4}})")
FOMC_RANGE_RE = re.compile(rf"({MONTH_RE})\s+(\d{{1,2}})(?:[-–](\d{{1,2}}))?\*?")


def _require_deps():
    if requests is None:
        raise RuntimeError("requests package not installed")
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 package not installed")


def _fetch_text(url: str, timeout: int = 20) -> str:
    _require_deps()
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (0DTE-calendar-refresh)"})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return soup.get_text(separator="\n")


def fetch_fomc_dates(year: int) -> list[date]:
    """Decision-day (second/last day of each 2-day meeting) FOMC dates for
    `year`, scraped from the Fed's own calendar page. That page lists
    BOTH the current and next year, so this asks for a specific year and
    only keeps dates whose month/day combination is plausible within a
    section attributable to that year (see the year-boundary handling
    below -- the page has no unambiguous per-line year marker for FOMC,
    since it groups meetings under a "20XX Meetings" heading instead of
    repeating the year on every line)."""
    text = _fetch_text("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")

    # BUG HISTORY (2026-09-09): originally searched for the bare year
    # number ("2027") as the section start, and read everything up to the
    # NEXT bare occurrence of "2028" as the section end. That's wrong --
    # the page mentions a given year in plenty of places besides the
    # actual meeting-dates heading (year-browse links, footers, archived
    # statements), so this grabbed a huge, wrong slice of the page and
    # parsed 96 "dates" out of it instead of ~8. Fixed by matching the
    # much more specific heading text "<year> Meetings" (as in "2027
    # Meetings"), which should only appear once, right where the actual
    # meeting list starts.
    start_marker = f"{year} Meetings"
    idx = text.find(start_marker)
    if idx == -1:
        raise ValueError(f"Could not find a '{start_marker}' heading on the Fed's FOMC calendar page "
                          f"-- the page's heading text may not match this exactly.")
    next_marker = f"{year + 1} Meetings"
    next_idx = text.find(next_marker, idx + len(start_marker))
    block = text[idx: next_idx if next_idx != -1 else idx + 2000]

    dates = []
    for m in FOMC_RANGE_RE.finditer(block):
        month_name, day1, day2 = m.groups()
        day = int(day2) if day2 else int(day1)  # decision day = last day of the meeting
        try:
            dates.append(date(year, MONTHS[month_name], day))
        except ValueError:
            continue  # skip anything that doesn't form a valid calendar date

    dates = sorted(set(dates))
    if not (6 <= len(dates) <= 10):  # the FOMC holds 8 meetings/year; allow slack for parse noise
        raise ValueError(f"Parsed an implausible number of FOMC dates for {year}: {len(dates)} -- "
                          f"page structure likely changed, not trusting this result: {dates}")
    return dates


def fetch_bls_dates(year: int, label: str, expected_count_range: tuple[int, int]) -> list[date]:
    """Release dates for one BLS series (label e.g. "Employment Situation",
    "Consumer Price Index", "Producer Price Index") for `year`, scraped
    from BLS's own full-year schedule page. Finds each occurrence of
    `label` in the page text and looks for a "Month D, YYYY" date within
    the next 80 characters -- markup-agnostic (works regardless of table/
    list structure) but assumes the date appears reasonably close to the
    label in the page's linear text, which held true as observed
    2026-09-09."""
    text = _fetch_text(f"https://www.bls.gov/schedule/{year}/home.htm")

    dates = []
    for m in re.finditer(re.escape(label), text):
        window = text[m.end(): m.end() + 80]
        dm = FULL_DATE_RE.search(window)
        if dm:
            month_name, day, yr = dm.groups()
            if int(yr) == year:
                dates.append(date(year, MONTHS[month_name], int(day)))

    dates = sorted(set(dates))
    lo, hi = expected_count_range
    if not (lo <= len(dates) <= hi):
        raise ValueError(f"Parsed an implausible number of '{label}' dates for {year}: "
                          f"{len(dates)} (expected {lo}-{hi}) -- page structure likely changed, "
                          f"not trusting this result: {dates}")
    return dates


def fetch_all(year: int) -> dict:
    """Fetches all four series for `year`. Raises on ANY failure -- see
    refresh_econ_calendar.py for how a partial/failed fetch is handled
    (never write a partial cache)."""
    return {
        "fomc": fetch_fomc_dates(year),
        "cpi": fetch_bls_dates(year, "Consumer Price Index", (10, 13)),
        "ppi": fetch_bls_dates(year, "Producer Price Index", (10, 14)),
        "nfp": fetch_bls_dates(year, "Employment Situation", (10, 13)),
    }
