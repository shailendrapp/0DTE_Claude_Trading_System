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

# Confirmed 2026 dates via the Fed's and BLS's own published schedules
# (checked 2026-09-08; covers Sep-Dec 2026 -- refresh/extend for the next
# year rather than assume this stays current). FOMC decision date is the
# second day of each 2-day meeting. NFP dates are also covered by the
# is_first_friday() heuristic below in every case checked, but are listed
# explicitly here since they were confirmed directly.
FOMC_DATES_2026 = [date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
                   date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
                   date(2026, 10, 28), date(2026, 12, 9)]
CPI_DATES_2026 = [date(2026, 9, 11), date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10)]
PPI_DATES_2026 = [date(2026, 9, 10), date(2026, 10, 15), date(2026, 11, 13)]
NFP_DATES_2026 = [date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4)]


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
