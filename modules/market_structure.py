"""
Overnight/opening market structure features: gap, VWAP, EMA structure,
ATR, realized volatility, and range acceptance/rejection. Pure numeric
functions on OHLCV bars -- no network calls, so these are unit-testable
offline (see the smoke test run alongside this delivery).

All of this feeds `scoring.py`'s composite trend score; nothing here makes
a trade decision by itself.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import statistics


def gap_pct(prior_close: float, today_open: float) -> float:
    return (today_open - prior_close) / prior_close * 100.0


def vwap_from_bars(bars: list[dict]) -> float:
    """bars: list of {high, low, close, volume}. Standard typical-price VWAP."""
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b.get("volume", 0) or 0
        cum_pv += typical * v
        cum_v += v
    if cum_v == 0:
        # no volume data (some index feeds omit it) -- fall back to simple average
        closes = [b["close"] for b in bars]
        return sum(closes) / len(closes) if closes else float("nan")
    return cum_pv / cum_v


def ema(values: list[float], span: int) -> list[float]:
    """Simple EMA, no pandas dependency needed for this small a job."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def ema_structure(daily_closes: list[float], fast: int = 9, slow: int = 21) -> dict:
    """daily_closes: most recent `slow`+ days of daily closes, oldest first,
    last element = most recent close (e.g. yesterday's, or today's running
    price if you want an intraday-updated read)."""
    if len(daily_closes) < slow:
        return {"fast": None, "slow": None, "aligned_bullish": None, "aligned_bearish": None}
    fast_ema = ema(daily_closes, fast)[-1]
    slow_ema = ema(daily_closes, slow)[-1]
    price = daily_closes[-1]
    return {
        "fast": fast_ema, "slow": slow_ema,
        "aligned_bullish": price > fast_ema > slow_ema,
        "aligned_bearish": price < fast_ema < slow_ema,
    }


def atr(bars: list[dict], period: int = 14) -> float:
    """bars: daily OHLC, oldest first, at least period+1 bars."""
    if len(bars) < period + 1:
        return float("nan")
    trs = []
    for i in range(1, len(bars)):
        h, l, prev_c = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs[-period:]) / period


def realized_vol_annualized(daily_closes: list[float], lookback: int = 10,
                             trading_days_per_year: int = 252) -> float:
    """Close-to-close annualized realized vol over the trailing `lookback`
    sessions. Distinct from VIX (which is IV-implied, forward-looking) --
    used together in vol_structure.py's IV-vs-RV comparison."""
    if len(daily_closes) < lookback + 1:
        return float("nan")
    window = daily_closes[-(lookback + 1):]
    log_rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    if len(log_rets) < 2:
        return float("nan")
    daily_sigma = statistics.stdev(log_rets)
    return daily_sigma * math.sqrt(trading_days_per_year)


def range_acceptance(current_price: float, prior_high: float, prior_low: float,
                      opening_range_high: float, opening_range_low: float) -> str:
    """Is price accepting (holding) or rejecting the prior day's range /
    today's opening range? A coarse but explainable classification, not a
    statistical model."""
    if current_price > prior_high and current_price > opening_range_high:
        return "accepting above prior range (breakout holding)"
    if current_price < prior_low and current_price < opening_range_low:
        return "accepting below prior range (breakdown holding)"
    if current_price > opening_range_high:
        return "testing above opening range, inside prior range"
    if current_price < opening_range_low:
        return "testing below opening range, inside prior range"
    return "inside both prior range and opening range (range-bound)"


@dataclass
class MarketStructure:
    gap_pct: float
    vwap: float
    price_vs_vwap: str          # "above" / "below" / "at"
    ema: dict
    atr_14: float
    realized_vol_annualized: float
    range_acceptance: str
    opening_range_high: float
    opening_range_low: float


def build_market_structure(prior_close: float, today_open: float, current_price: float,
                            opening_range_bars: list[dict], daily_closes_incl_today: list[float],
                            daily_bars_for_atr: list[dict], prior_high: float, prior_low: float) -> MarketStructure:
    vwap = vwap_from_bars(opening_range_bars)
    price_vs_vwap = "above" if current_price > vwap else ("below" if current_price < vwap else "at")
    orh = max(b["high"] for b in opening_range_bars)
    orl = min(b["low"] for b in opening_range_bars)
    return MarketStructure(
        gap_pct=gap_pct(prior_close, today_open),
        vwap=vwap,
        price_vs_vwap=price_vs_vwap,
        ema=ema_structure(daily_closes_incl_today),
        atr_14=atr(daily_bars_for_atr),
        realized_vol_annualized=realized_vol_annualized(daily_closes_incl_today),
        range_acceptance=range_acceptance(current_price, prior_high, prior_low, orh, orl),
        opening_range_high=orh, opening_range_low=orl,
    )
