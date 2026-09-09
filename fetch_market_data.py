#!/usr/bin/env python3
"""
Run this LOCALLY on your own machine, not in the Claude sandbox -- Yahoo
Finance is blocked from that sandbox's network (confirmed: 403 on every
attempt), but your own machine should reach it fine.

Downloads SPX (^GSPC) and VIX (^VIX) daily history and writes them in the
exact CSV shape run_backtest.py expects:

    date,open,high,low,close      (spx_daily.csv)
    date,open,high,low,close      (vix_daily.csv -- "close" here is the VIX level)

Usage:
    pip install yfinance pandas
    python fetch_market_data.py --years 10
    python fetch_market_data.py --start 2016-01-01 --end 2026-09-08

Then, back wherever you're running the backtest:
    python run_backtest.py --spx spx_daily.csv --vix vix_daily.csv

Notes / known limitations of Yahoo Finance data for this purpose:
- ^GSPC is the S&P 500 INDEX, not SPY -- correct choice for SPX-based
  strikes (no ETF tracking-error / dividend-drag issues SPY has).
- ^VIX is the actual CBOE VIX index (spot), not a futures-based ETF proxy
  like VIXY -- this is the right series, better than the VIXY workaround
  discussed earlier.
- This is still only OHLC (daily), not the SPX 0DTE option chain itself --
  the backtest engine prices options synthetically off these OHLC + VIX
  via Black-Scholes with a fill haircut (see modules/option_pricer.py). If
  you want a fill-accurate backtest eventually, you need real historical
  0DTE options quotes (e.g. from your Massive/Polygon connector once
  indices/options entitlements are on the plan, or CBOE DataShop), not
  just index OHLC -- this script does not attempt to get you that.
- Yahoo occasionally rate-limits or requires a cookie/crumb handshake that
  intermittently fails; if a download comes back empty, wait a bit and
  rerun -- the script retries a few times automatically.
"""
from __future__ import annotations
import argparse
import datetime as dt
import sys
import time

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency. Run: pip install yfinance pandas")

import pandas as pd


def download_with_retry(ticker: str, start: str, end: str, retries: int = 3, pause: float = 3.0) -> pd.DataFrame:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                return df
            last_err = RuntimeError(f"{ticker}: empty response")
        except Exception as e:  # noqa: BLE001
            last_err = e
        print(f"  attempt {attempt}/{retries} failed for {ticker} ({last_err}); retrying...")
        time.sleep(pause)
    raise RuntimeError(f"Failed to download {ticker} after {retries} attempts: {last_err}")


def to_ohlc_csv(df: pd.DataFrame, out_path: str, ticker_label: str) -> None:
    # yfinance sometimes returns MultiIndex columns (Ticker, Field) -- flatten.
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]

    out = pd.DataFrame({
        "date": df.index.strftime("%Y-%m-%d"),
        "open": df["Open"].values,
        "high": df["High"].values,
        "low": df["Low"].values,
        "close": df["Close"].values,
    })
    out.to_csv(out_path, index=False)
    print(f"Wrote {len(out)} rows for {ticker_label} -> {out_path} "
          f"({out['date'].iloc[0]} to {out['date'].iloc[-1]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10, help="years of history back from --end (or today)")
    ap.add_argument("--start", help="explicit start date YYYY-MM-DD, overrides --years")
    ap.add_argument("--end", default=dt.date.today().isoformat(), help="end date YYYY-MM-DD, default today")
    ap.add_argument("--spx-out", default="spx_daily.csv")
    ap.add_argument("--vix-out", default="vix_daily.csv")
    args = ap.parse_args()

    if args.start:
        start = args.start
    else:
        end_d = dt.date.fromisoformat(args.end)
        start = (end_d - dt.timedelta(days=365 * args.years + 5)).isoformat()

    print(f"Downloading ^GSPC (SPX) from {start} to {args.end} ...")
    spx = download_with_retry("^GSPC", start, args.end)
    to_ohlc_csv(spx, args.spx_out, "SPX (^GSPC)")

    print(f"Downloading ^VIX from {start} to {args.end} ...")
    vix = download_with_retry("^VIX", start, args.end)
    to_ohlc_csv(vix, args.vix_out, "VIX (^VIX)")

    print("\nDone. Run the backtest against real data with:")
    print(f"    python run_backtest.py --spx {args.spx_out} --vix {args.vix_out}")


if __name__ == "__main__":
    main()
