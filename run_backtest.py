#!/usr/bin/env python3
"""
CLI: run the 10-year (or however-many-year) backtest.

Real data (recommended):
    python run_backtest.py --spx spx_daily.csv --vix vix_daily.csv

Each CSV needs columns: date, open, high, low, close (vix csv just needs
date, close -- the level). If either file is omitted, falls back to the
clearly-labeled SYNTHETIC demonstration path -- see README section 6 for
why this sandbox couldn't fetch real history itself.
"""
import argparse
import datetime as dt
import os

import pandas as pd

import config
from modules.data_sources import load_csv_series, synthetic_spx_vix_path
from modules.backtest_engine import run_backtest, summarize
from modules.econ_calendar import FOMC_DATES_2026, CPI_DATES_2026


def config_note() -> str:
    return (f"{config.FILL_HAIRCUT} (i.e. assume you realize this fraction of Black-Scholes "
            "theoretical credit -- calibrate from your own real fill logs, e.g. Vajra's).")


def build_dataframe(spx_path: str | None, vix_path: str | None, years: int) -> tuple[pd.DataFrame, bool]:
    if spx_path and vix_path:
        spx = load_csv_series(spx_path)
        vix = load_csv_series(vix_path)[["date", "close"]].rename(columns={"close": "vix"})
        merged = pd.merge(spx, vix, on="date", how="inner")
        return merged, False
    return synthetic_spx_vix_path(years=years), True


def fomc_cpi_placeholder(years: int):
    """
    Real FOMC/CPI dates are only loaded for 2026 (confirmed against the
    Fed's and BLS's own published schedules -- see econ_calendar.py). Years
    before 2026 in a 10-year backtest still have no event dates loaded --
    that's a real gap, not a design choice; backfilling the full historical
    FOMC/CPI calendar back to 2016 would close it and is straightforward
    (both are public record) but wasn't done here. Effect on results so
    far: none of the backtest runs in this project have shown a difference
    from this gap, since the event-delay filter never fires outside the
    dates it's given.
    """
    return FOMC_DATES_2026, CPI_DATES_2026


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spx", help="CSV path: date,open,high,low,close for SPX (or SPY, scaled)")
    ap.add_argument("--vix", help="CSV path: date,close for VIX level")
    ap.add_argument("--years", type=int, default=10, help="years of synthetic data if no CSVs given")
    ap.add_argument("--out", default="reports", help="output directory for the report")
    args = ap.parse_args()

    df, is_synthetic = build_dataframe(args.spx, args.vix, args.years)
    fomc_dates, cpi_dates = fomc_cpi_placeholder(args.years)

    results = run_backtest(df, fomc_dates=fomc_dates, cpi_dates=cpi_dates)
    stats = summarize(results)

    os.makedirs(args.out, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_out = os.path.join(args.out, f"backtest_daily_{ts}.csv")
    report_out = os.path.join(args.out, f"backtest_report_{ts}.md")
    results.to_csv(csv_out, index=False)

    with open(report_out, "w") as f:
        f.write(f"# 0DTE SPX Strategy Backtest Report\n\n")
        f.write(f"Generated: {dt.datetime.now().isoformat()}\n\n")
        if is_synthetic:
            f.write("## ⚠️ DATA SOURCE: SYNTHETIC (SIMULATED), NOT REAL MARKET HISTORY\n\n")
            f.write("No `--spx`/`--vix` CSVs were provided, so this run used a GBM-based "
                    "synthetic SPX/VIX path (see modules/data_sources.py:synthetic_spx_vix_path). "
                    "**These numbers describe the simulation, not the market. Do not use them to "
                    "decide whether to trade this strategy live.** Supply real historical CSVs "
                    "to get a real answer.\n\n")
        else:
            f.write("## Data source: user-supplied CSV (real data)\n\n")
            f.write(f"SPX file: `{args.spx}`  \nVIX file: `{args.vix}`\n\n")
            f.write("Note: trend classification still uses the daily-bar proxy in "
                    "`modules/market_open.py:classify_from_daily_bar_proxy` unless intraday "
                    "1-minute bars are supplied separately -- see README section 6.\n\n")

        f.write("## Summary statistics\n\n")
        for k, v in stats.items():
            f.write(f"- **{k}**: {v}\n")

        f.write("\n## Reminder\n\n")
        f.write("Win rate alone is not evidence of an edge for 0DTE credit strategies -- "
                "check `expectancy_per_trade`, `profit_factor`, and `max_drawdown` together. "
                "A high win rate with negative or near-zero expectancy is the well-documented "
                "failure mode this system is designed to avoid, not chase.\n\n")
        f.write("## Parameters that drove this specific result (unvalidated, calibrate before trusting)\n\n")
        f.write(f"- Wing width: 10 points, fixed (from the CBOE Schwartz example spread width -- "
                "not necessarily right for every VIX/expected-move regime; too narrow relative to "
                "the realized move and every structure loses almost by construction).\n")
        f.write(f"- Fill haircut: {config_note()}\n")
        f.write("- Breakout confirmation fraction and opening-range window: see config.py.\n"
                "- The daily-bar proxy trend classifier only sees the overnight gap (by design, to "
                "avoid lookahead bias -- see modules/market_open.py) which is a materially weaker "
                "signal than a true intraday opening-range read, so this run's structure mix "
                "(mostly Iron Fly/Balanced IC, rarely a breakout structure) understates how often "
                "a live, intraday version of this system would use the skewed/single-side structures.\n")

    print(f"Wrote daily results to {csv_out}")
    print(f"Wrote report to {report_out}")
    print()
    for k, v in stats.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
