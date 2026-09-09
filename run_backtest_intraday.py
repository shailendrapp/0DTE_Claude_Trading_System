#!/usr/bin/env python3
"""
CLI: the intraday, multi-window, stacking-lots backtest -- models your
actual live design (2-3 trades/day at fixed windows, stacking allowed, 3
scale-out lots per signal with a ratcheting stop) rather than
run_backtest.py's single-signal, hold-to-close model.

Real data (recommended):
    python run_backtest_intraday.py --spx spx_daily.csv --vix vix_daily.csv

Falls back to the same synthetic demonstration path as run_backtest.py if
CSVs are omitted. IMPORTANT: this backtest additionally requires a
simulated intraday price path (modules/intraday_path.py) since no real
intraday history was available for the full window -- read that module's
docstring before trusting exact numbers, especially exit timing.
"""
import argparse
import datetime as dt
import os

import pandas as pd

import config
from modules.data_sources import load_csv_series, synthetic_spx_vix_path
from modules.backtest_intraday import run_intraday_backtest, summarize_intraday
from modules.econ_calendar import FOMC_DATES_2026, CPI_DATES_2026, PPI_DATES_2026, NFP_DATES_2026


def build_dataframe(spx_path, vix_path, years):
    if spx_path and vix_path:
        spx = load_csv_series(spx_path)
        vix = load_csv_series(vix_path)[["date", "close"]].rename(columns={"close": "vix"})
        return pd.merge(spx, vix, on="date", how="inner"), False
    return synthetic_spx_vix_path(years=years), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spx")
    ap.add_argument("--vix")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--out", default="reports")
    ap.add_argument("--step-minutes", type=int, default=5)
    args = ap.parse_args()

    df, is_synthetic = build_dataframe(args.spx, args.vix, args.years)

    results = run_intraday_backtest(df, fomc_dates=FOMC_DATES_2026, cpi_dates=CPI_DATES_2026,
                                     ppi_dates=PPI_DATES_2026, nfp_dates=NFP_DATES_2026,
                                     step_minutes=args.step_minutes)
    stats = summarize_intraday(results)

    os.makedirs(args.out, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_out = os.path.join(args.out, f"backtest_intraday_lots_{ts}.csv")
    report_out = os.path.join(args.out, f"backtest_intraday_report_{ts}.md")
    results.to_csv(csv_out, index=False)

    with open(report_out, "w") as f:
        f.write("# 0DTE SPX Intraday Multi-Window Backtest Report\n\n")
        f.write(f"Generated: {dt.datetime.now().isoformat()}\n\n")
        if is_synthetic:
            f.write("## ⚠️ PRICE DATA: SYNTHETIC (SIMULATED), NOT REAL MARKET HISTORY\n\n")
        else:
            f.write("## Daily price data: user-supplied CSV (real SPX/VIX)\n\n")
            f.write(f"SPX file: `{args.spx}`  \nVIX file: `{args.vix}`\n\n")
        f.write("## ⚠️ INTRADAY PATH: SIMULATED, constrained to match real daily OHLC\n\n")
        f.write("No real intraday (minute-level) history was available for the full "
                "backtest window (see README §6/7f) -- every day's path is a simulated "
                "4-anchor bridge (open -> low/high in an assumed order -> close) with "
                "Brownian-bridge noise, clipped to the day's real high/low. This is what "
                "lets lot-level profit-target/stop/strike-test exits be tested at all, "
                "but exact exit TIMING and whether a specific intraday touch actually "
                "happened is a model assumption, not observed fact. See "
                "`modules/intraday_path.py` for the exact method.\n\n")

        f.write("## Summary statistics (per LOT, not per day -- a day can contribute up to "
                f"{config.NUM_LOTS * len(config.ENTRY_WINDOWS)} lots across "
                f"{len(config.ENTRY_WINDOWS)} windows)\n\n")
        for k, v in stats.items():
            f.write(f"- **{k}**: {v}\n")

        f.write("\n## Reminder\n\n")
        f.write("This models the STACKING design (multiple concurrent windows, no cap by "
                "default) -- compare n_signals here to the single-signal run_backtest.py "
                "result to see the actual trade-count multiplier, and compare total_pnl and "
                "max_drawdown directly against that run's numbers, not just win rate.\n")

    print(f"Wrote per-lot results to {csv_out}")
    print(f"Wrote report to {report_out}")
    print()
    for k, v in stats.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
