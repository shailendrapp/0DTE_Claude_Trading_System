"""
Data sourcing: a thin Tradier client for live use, and a clearly-labeled
synthetic SPX/VIX path generator for demonstrating the backtest pipeline
when real historical data isn't available in the running environment.

IMPORTANT: synthetic_spx_vix_path() does NOT produce real market history.
It exists solely so run_backtest.py has something to execute against out of
the box. Always prefer --spx/--vix CSV inputs of real data. See README §6.
"""
from __future__ import annotations
import math
import os
import random
from dataclasses import dataclass

import pandas as pd

import config

try:
    import requests
except ImportError:
    requests = None


@dataclass
class TradierClient:
    token: str
    sandbox: bool = True

    @property
    def base_url(self) -> str:
        return config.TRADIER_BASE_URL_SANDBOX if self.sandbox else config.TRADIER_BASE_URL_PROD

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def get_quote(self, symbol: str) -> dict:
        if requests is None:
            raise RuntimeError("requests package not installed")
        r = requests.get(f"{self.base_url}/markets/quotes",
                          params={"symbols": symbol}, headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_option_chain(self, symbol: str, expiration: str) -> dict:
        if requests is None:
            raise RuntimeError("requests package not installed")
        r = requests.get(f"{self.base_url}/markets/options/chains",
                          params={"symbol": symbol, "expiration": expiration, "greeks": "true"},
                          headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_history(self, symbol: str, interval: str, start: str, end: str) -> dict:
        if requests is None:
            raise RuntimeError("requests package not installed")
        r = requests.get(f"{self.base_url}/markets/history",
                          params={"symbol": symbol, "interval": interval, "start": start, "end": end},
                          headers=self._headers(), timeout=10)
        r.raise_for_status()
        return r.json()


def parse_tradier_chain(raw_chain_json: dict) -> list[dict]:
    """
    Adapts Tradier's /markets/options/chains response into the flat
    {strike, option_type, delta, iv, open_interest, gamma} shape
    vol_structure.py expects. UNTESTED against a live response -- this
    sandbox has no network path to Tradier -- so treat this as a strong
    draft: print/inspect one real response and adjust field paths here if
    Tradier's actual shape differs (their docs describe
    options.option[].{strike, option_type, open_interest,
    greeks.{delta, gamma, mid_iv}}, which is what this assumes).
    """
    options = raw_chain_json.get("options", {}).get("option", [])
    if isinstance(options, dict):
        options = [options]
    out = []
    for o in options:
        greeks = o.get("greeks") or {}
        out.append({
            "strike": o.get("strike"),
            "option_type": o.get("option_type"),
            "open_interest": o.get("open_interest"),
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "iv": greeks.get("mid_iv") or greeks.get("smv_vol"),
        })
    return out


def load_csv_series(path: str) -> pd.DataFrame:
    """Expects columns: date, open, high, low, close (case-insensitive)."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"date", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def synthetic_spx_vix_path(years: int = 10, start_price: float = 4200.0,
                            start_vix: float = 16.0, seed: int = 42) -> pd.DataFrame:
    """
    SIMULATED DATA -- NOT REAL MARKET HISTORY.
    GBM-with-vol-clustering approximation calibrated loosely to long-run SPX
    stats (annual drift ~8%, base vol ~16%, VIX mean-reverting with spikes).
    Exists only to exercise the backtest pipeline end to end.
    """
    rng = random.Random(seed)
    n_days = years * config.TRADING_DAYS_PER_YEAR
    dates = pd.bdate_range(end=pd.Timestamp.today(), periods=n_days)

    mu = 0.08 / config.TRADING_DAYS_PER_YEAR
    base_sigma_annual = 0.16
    vix = start_vix
    price = start_price

    rows = []
    for d in dates:
        # VIX: mean-reverting with occasional spikes (fat tail proxy)
        vix += (16.0 - vix) * 0.03 + rng.gauss(0, 1.2)
        if rng.random() < 0.01:  # ~1% of days, a vol spike (tail event proxy)
            vix += rng.uniform(8, 25)
        vix = max(9.0, min(vix, 85.0))

        daily_sigma = (vix / 100.0) / math.sqrt(config.TRADING_DAYS_PER_YEAR)
        # fatter-tailed shock: mixture of normal + occasional jump
        shock = rng.gauss(0, 1)
        if rng.random() < 0.015:
            shock += rng.choice([-1, 1]) * rng.uniform(3, 8)  # jump day

        ret = mu + daily_sigma * shock
        open_p = price
        close_p = price * math.exp(ret)
        intraday_range = abs(close_p - open_p) + price * daily_sigma * rng.uniform(0.5, 1.8)
        high_p = max(open_p, close_p) + intraday_range * rng.uniform(0.1, 0.4)
        low_p = min(open_p, close_p) - intraday_range * rng.uniform(0.1, 0.4)

        rows.append({"date": d, "open": open_p, "high": high_p, "low": low_p,
                      "close": close_p, "vix": vix})
        price = close_p

    return pd.DataFrame(rows)
