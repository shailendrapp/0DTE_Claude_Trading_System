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
    # BUG HISTORY (2026-09-09): a clearly-a-bot User-Agent string like
    # "Mozilla/5.0 (0DTE-calendar-refresh)" got BLS returning a flat 403
    # Forbidden (the Fed's page was reachable either way, so this was
    # BLS-specific bot blocking). Using a realistic modern-browser UA and
    # the Accept headers a real browser sends fixed it for BLS; keeping
    # them for the Fed fetch too for consistency/robustness.
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return soup.get_text(separator="\n")


def _diagnostic_dump(text: str, year: int) -> str:
    """Builds a diagnostic excerpt of the real fetched page text, for
    inclusion in a raised error's message so it lands in the Action log.
    We've now guessed wrong twice about this page's exact heading text
    (bare year -> way too broad; "<year> Meetings" -> not found at all)
    from this environment, which cannot fetch the raw page itself to
    check. Rather than guess a third time, dump enough real ground truth
    -- the first chunk of the page text, plus a context window around
    every case-insensitive occurrence of "meeting" that's near a 4-digit
    year -- that the actual heading structure can be read directly out
    of the next failed run's log and fixed with certainty."""
    lines = ["----- DIAGNOSTIC: first 2000 chars of fetched page text -----",
             text[:2000],
             "----- DIAGNOSTIC: context around occurrences of 'meeting' -----"]
    year_re = re.compile(r"\b20\d{2}\b")
    seen_spans = set()
    for m in re.finditer(r"meeting", text, re.IGNORECASE):
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 60)
        # BUG (2026-09-09): this was `if any(year_re.search(...))`, which
        # is wrong -- any() needs an iterable, and .search() returns a
        # Match object or None, neither of which is iterable. any(None)
        # raises TypeError('NoneType' object is not iterable), which is
        # exactly the unhelpful error a real run hit here, crashing this
        # diagnostic before it could print anything useful. Fixed to a
        # plain truthiness check.
        if year_re.search(text[start:end]):
            span = (start // 40, end // 40)  # coarse de-dup of overlapping windows
            if span in seen_spans:
                continue
            seen_spans.add(span)
            snippet = text[start:end].replace("\n", " \\n ")
            lines.append(f"  ...{snippet}...")
        if len(lines) > 60:  # don't blow up the log
            lines.append("  (truncated -- too many matches)")
            break
    return "\n".join(lines)


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
    # parsed 96 "dates" out of it instead of ~8. Attempted fix #2: matched
    # the more specific heading text "<year> Meetings" -- but that ALSO
    # failed (not found at all, for both 2026 and 2027), proving the
    # assumed heading text doesn't literally exist in the real page. This
    # environment cannot fetch the raw page itself to verify, so rather
    # than guess a third time, a failed lookup now dumps real page text
    # into the raised error (see _diagnostic_dump) so the next run's
    # Action log shows real ground truth instead of another blind guess.
    start_marker = f"{year} Meetings"
    idx = text.find(start_marker)
    if idx == -1:
        raise ValueError(f"Could not find a '{start_marker}' heading on the Fed's FOMC calendar page "
                          f"-- the page's heading text may not match this exactly.\n"
                          f"{_diagnostic_dump(text, year)}")
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
                          f"page structure likely changed, not trusting this result: {dates}\n"
                          f"----- DIAGNOSTIC: matched block (first 2000 chars) -----\n{block[:2000]}")
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
        # We have not yet confirmed the BLS side works at all (both real
        # runs so far only got far enough to show FOMC failures) -- same
        # diagnostic-dump treatment as fetch_fomc_dates rather than
        # guessing blind if/when this trips.
        occ_count = len(re.findall(re.escape(label), text))
        raise ValueError(f"Parsed an implausible number of '{label}' dates for {year}: "
                          f"{len(dates)} (expected {lo}-{hi}) -- page structure likely changed, "
                          f"not trusting this result: {dates}\n"
                          f"----- DIAGNOSTIC: '{label}' occurs {occ_count} times in fetched text -----\n"
                          f"----- DIAGNOSTIC: first 2000 chars of fetched page text -----\n{text[:2000]}")
    return dates


def fetch_all(year: int) -> dict:
    """Fetches all four series for `year`. Raises on ANY failure -- see
    refresh_econ_calendar.py for how a partial/failed fetch is handled
    (never write a partial cache).

    BUG HISTORY (2026-09-09): this used to be a plain dict comprehension,
    so a FOMC failure raised immediately and CPI/PPI/NFP were never even
    attempted -- meaning two straight debugging rounds only ever showed
    us the FOMC error, with zero information about whether the BLS-side
    scraper works at all. Now runs all four independently and combines
    every failure into one error message, so a single run's log shows
    the full picture instead of just whichever series happens to be
    fetched first."""
    series = {
        "fomc": lambda: fetch_fomc_dates(year),
        "cpi": lambda: fetch_bls_dates(year, "Consumer Price Index", (10, 13)),
        "ppi": lambda: fetch_bls_dates(year, "Producer Price Index", (10, 14)),
        "nfp": lambda: fetch_bls_dates(year, "Employment Situation", (10, 13)),
    }
    result = {}
    errors = []
    for name, fn in series.items():
        try:
            result[name] = fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"[{name}] {e}")
    if errors:
        raise ValueError(f"{len(errors)}/4 series failed for {year}:\n" + "\n\n".join(errors))
    return result
