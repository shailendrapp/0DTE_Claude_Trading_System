"""
Persistent trade P&L log across days, so daily/weekly/monthly Telegram
summaries can be produced from something that outlives a single day's
state files (which get renamed to .done and are otherwise never
aggregated anywhere).

Append-only CSV (state/pnl_log.csv by default) -- one row per fully-closed
DAILY SIGNAL (i.e. per entry window per day, not per lot), written by
run_monitor.py the moment every lot in that signal is closed.

Simplification, documented rather than hidden: "weekly" = calendar Mon-Fri
and "monthly" = calendar month, using the trading dates actually present in
the log -- a holiday-shortened week/month still aggregates correctly. What
IS approximate is the two is_last_*() triggers below, which assume the
last trading day of the week/month is the last weekday (Fri / last Mon-Fri
of the month) with no market-holiday calendar. A holiday landing on that
exact day (e.g. Good Friday, or a Monday holiday that isn't month-end)
means that period's auto-triggered summary either fires a day early/late
or not at all that specific week/month -- run modules/pnl_log.py's summary
functions manually against the log if that ever matters.
"""
from __future__ import annotations
import csv
import datetime as dt
import os

LOG_PATH = os.path.join("state", "pnl_log.csv")
FIELDS = ["date", "window", "structure", "total_pnl", "n_lots"]


def append_signal(date_str: str, window: str, structure: str, total_pnl: float,
                   n_lots: int, path: str = LOG_PATH) -> None:
    dir_ = os.path.dirname(path)
    if dir_:
        os.makedirs(dir_, exist_ok=True)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({"date": date_str, "window": window, "structure": structure,
                    "total_pnl": round(total_pnl, 2), "n_lots": n_lots})


def load_log(path: str = LOG_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["date_obj"] = dt.date.fromisoformat(r["date"])
        r["total_pnl"] = float(r["total_pnl"])
        r["n_lots"] = int(r["n_lots"])
    return rows


def _filter(rows: list[dict], start: dt.date, end: dt.date) -> list[dict]:
    return [r for r in rows if start <= r["date_obj"] <= end]


def _aggregate(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"label": label, "n_days": 0, "n_signals": 0, "n_lots": 0,
                 "total_pnl": 0.0, "wins": 0, "losses": 0, "win_rate_pct": 0.0}
    total_pnl = sum(r["total_pnl"] for r in rows)
    n_lots = sum(r["n_lots"] for r in rows)
    wins = sum(1 for r in rows if r["total_pnl"] > 0)
    losses = sum(1 for r in rows if r["total_pnl"] <= 0)
    return {
        "label": label,
        "n_days": len(set(r["date"] for r in rows)),
        "n_signals": len(rows),
        "n_lots": n_lots,
        "total_pnl": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100 * wins / len(rows), 1),
    }


def daily_summary(rows: list[dict], date: dt.date) -> dict:
    return _aggregate(_filter(rows, date, date), f"Daily Summary — {date}")


def weekly_summary(rows: list[dict], ref_date: dt.date) -> dict:
    start = ref_date - dt.timedelta(days=ref_date.weekday())  # Monday
    end = start + dt.timedelta(days=4)                        # Friday
    return _aggregate(_filter(rows, start, end), f"Weekly Summary — {start} to {end}")


def monthly_summary(rows: list[dict], ref_date: dt.date) -> dict:
    start = ref_date.replace(day=1)
    return _aggregate(_filter(rows, start, ref_date), f"Monthly Summary — {start.strftime('%B %Y')}")


def is_last_weekday_of_week(d: dt.date) -> bool:
    """Friday, approximated with no market-holiday calendar -- see module
    docstring."""
    return d.weekday() == 4


def is_last_trading_day_of_month(d: dt.date) -> bool:
    """True if the next weekday falls in a different month (no
    market-holiday calendar -- see module docstring)."""
    next_day = d + dt.timedelta(days=1)
    while next_day.weekday() >= 5:  # skip Sat/Sun
        next_day += dt.timedelta(days=1)
    return next_day.month != d.month
