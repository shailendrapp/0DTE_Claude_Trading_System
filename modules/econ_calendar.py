"""
Econ calendar filter.

Live use: refresh fomc_dates/cpi_dates from a free calendar source (e.g. the
Federal Reserve's own FOMC schedule page, BLS release schedule for
NFP/CPI -- both publish fixed schedules far in advance, no scraping
of paid calendars needed).

Time-of-day aware (needed once entries can happen at any of several windows
during the day, not just near the open): FOMC releases at 2pm ET, CPI/NFP
at 8:30am ET. A window's entry is blocked only if it falls within
config.PRE_EVENT_BLACKOUT_MINUTES before, or config.EVENT_DELAY_MINUTES_AFTER_RELEASE
after, the actual release clock time -- not just "near the open" as an
earlier, simpler version of this module assumed.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time
from enum import Enum
import json
import os
import config


class EventType(str, Enum):
    FOMC = "FOMC"
    NFP = "NFP"
    CPI = "CPI"
    PPI = "PPI"
    OPEX = "OPEX"
    NONE = "NONE"


RELEASE_TIME_ET = {
    EventType.FOMC: dt_time(14, 0),
    EventType.CPI: dt_time(8, 30),
    EventType.NFP: dt_time(8, 30),
    EventType.PPI: dt_time(8, 30),
}

# Full-year 2026 dates. FOMC confirmed directly from the Fed's own
# calendar page (via econ_calendar_fetch.py's live scraper -- see that
# module's fetch_fomc_dates for the ground-truth page structure this was
# checked against, 2026-09-09). CPI/PPI/NFP updated 2026-09-09 from
# BLS's own published schedule pages (bls.gov/schedule/news_release/
# cpi.htm, ppi.htm, empsit.htm) -- BLS's site currently 403s the live
# scraper (bot-management, not a parsing bug; see fetch_bls_dates/
# _fetch_text_bls), so THIS hardcoded list is the only source for
# CPI/PPI/NFP until that's resolved. FOMC decision date is the second
# day of each 2-day meeting. NFP dates are also covered by the
# is_first_friday() heuristic below in every case checked, but are
# listed explicitly since they were confirmed directly. Re-verify/
# extend for next year rather than assume this stays current.
FOMC_DATES_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
                   date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
                   date(2026, 10, 28), date(2026, 12, 9)]
CPI_DATES_2026 = [date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11), date(2026, 4, 10),
                   date(2026, 5, 12), date(2026, 6, 10), date(2026, 7, 14), date(2026, 8, 12),
                   date(2026, 9, 11), date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10)]
PPI_DATES_2026 = [date(2026, 1, 14), date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 18),
                   date(2026, 4, 14), date(2026, 5, 13), date(2026, 6, 11), date(2026, 7, 15),
                   date(2026, 8, 13), date(2026, 9, 10), date(2026, 10, 15), date(2026, 11, 13),
                   date(2026, 12, 15)]
NFP_DATES_2026 = [date(2026, 1, 9), date(2026, 2, 11), date(2026, 3, 6), date(2026, 4, 3),
                   date(2026, 5, 8), date(2026, 6, 5), date(2026, 7, 2), date(2026, 8, 7),
                   date(2026, 9, 4), date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4)]

CALENDAR_CACHE_PATH = os.path.join("data", "econ_calendar_cache.json")


def load_calendar_dates(cache_path: str = CALENDAR_CACHE_PATH) -> dict:
    """Preferred source of truth for run_live.py: the cache built by
    refresh_econ_calendar.py (see modules/econ_calendar_fetch.py), which
    is scraped from the Fed's/BLS's own published schedules on a slow
    (weekly) cadence and covers whichever years have actually been
    fetched -- including years beyond 2026, which the hardcoded lists
    above never will. Falls back to the hardcoded FOMC/CPI/PPI/NFP_DATES_
    2026 lists (merged in for 2026 specifically) if the cache file
    doesn't exist yet, is corrupt, or is simply missing a given year --
    this function never raises; a missing/bad cache degrades to
    "2026 only, manually maintained" rather than breaking event-risk
    gating entirely.

    Returns {"fomc": [date,...], "cpi": [...], "ppi": [...], "nfp": [...]}
    across ALL cached years combined (check_event() only cares whether
    today's date is IN the list, so a flat multi-year list is fine)."""
    fomc, cpi, ppi, nfp = list(FOMC_DATES_2026), list(CPI_DATES_2026), list(PPI_DATES_2026), list(NFP_DATES_2026)

    try:
        with open(cache_path) as f:
            cached = json.load(f)
        for year_str, series in cached.get("years", {}).items():
            for key, target in (("fomc", fomc), ("cpi", cpi), ("ppi", ppi), ("nfp", nfp)):
                for iso in series.get(key, []):
                    d = date.fromisoformat(iso)
                    if d not in target:
                        target.append(d)
    except FileNotFoundError:
        pass  # no cache built yet -- 2026 hardcoded lists are still a valid fallback
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[warning] econ calendar cache at {cache_path} is corrupt/unreadable ({e}) -- "
              f"falling back to hardcoded 2026 dates only.")

    return {"fomc": sorted(set(fomc)), "cpi": sorted(set(cpi)),
            "ppi": sorted(set(ppi)), "nfp": sorted(set(nfp))}


@dataclass
class EventFlag:
    event_type: EventType
    is_event_day: bool
    minutes_since_release: float | None  # negative if release is later today; None if not applicable
    note: str


def is_first_friday(d: date) -> bool:
    return d.weekday() == 4 and d.day <= 7


def is_monthly_opex(d: date) -> bool:
    """3rd Friday of the month -- standard monthly options expiration."""
    if d.weekday() != 4:
        return False
    return 15 <= d.day <= 21


def check_event(now_et: datetime, fomc_dates: list[date], cpi_dates: list[date],
                 ppi_dates: list[date] | None = None, nfp_dates: list[date] | None = None) -> EventFlag:
    """
    fomc_dates / cpi_dates / ppi_dates / nfp_dates: maintain these from the
    Fed's and BLS's published schedules (both released far in advance --
    pull fresh each quarter; see FOMC_DATES_2026 etc. above for a starting
    point through end of 2026). now_et: the current decision time, in
    US/Eastern (whichever entry window is being evaluated) -- NOT
    necessarily market open. If nfp_dates is omitted, falls back to the
    is_first_friday() heuristic (matches every confirmed 2026 date, but an
    explicit list is more reliable across holiday-shifted months).
    """
    today = now_et.date()
    ppi_dates = ppi_dates or []
    nfp_explicit = nfp_dates is not None

    if today in fomc_dates:
        event_type = EventType.FOMC
    elif today in cpi_dates:
        event_type = EventType.CPI
    elif today in ppi_dates:
        event_type = EventType.PPI
    elif (today in nfp_dates) if nfp_explicit else is_first_friday(today):
        event_type = EventType.NFP
    elif is_monthly_opex(today):
        return EventFlag(EventType.OPEX, True, None,
                          "Monthly OPEX -- unusual pinning/flow dynamics, reduce size.")
    else:
        return EventFlag(EventType.NONE, False, None, "No scheduled high-impact event.")

    release_dt = datetime.combine(today, RELEASE_TIME_ET[event_type], tzinfo=now_et.tzinfo)
    minutes_relative = (now_et - release_dt).total_seconds() / 60.0
    when = "before" if minutes_relative < 0 else "after"
    note = (f"{event_type.value} release at {RELEASE_TIME_ET[event_type].strftime('%H:%M')} ET -- "
            f"this decision point is {abs(minutes_relative):.0f} min {when} it.")
    return EventFlag(event_type, True, minutes_relative, note)


def should_delay_entry(flag: EventFlag) -> bool:
    if flag.event_type not in (EventType.FOMC, EventType.NFP, EventType.CPI, EventType.PPI):
        return False
    if flag.minutes_since_release is None:
        return True  # be conservative if we somehow don't know the timing
    return -config.PRE_EVENT_BLACKOUT_MINUTES <= flag.minutes_since_release < config.EVENT_DELAY_MINUTES_AFTER_RELEASE


def size_multiplier(flag: EventFlag) -> float:
    if flag.event_type == EventType.OPEX:
        return config.OPEX_SIZE_MULTIPLIER
    return 1.0
