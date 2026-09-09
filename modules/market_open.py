"""
Opening-range trend classifier.

Live use: feed it 30 minutes of 1-min SPX bars from Tradier after the open.
Backtest use (daily-bar data only, see backtest_engine.py): approximates the
opening range using the day's open vs. prior close and the day's own
high/low as a coarse proxy, and says so in the report -- this is a real
limitation of free daily-only historical data, not a design choice.
"""
from dataclasses import dataclass
from enum import Enum
import config


class Trend(str, Enum):
    RANGE_BOUND = "range_bound"
    WEAK_BREAKOUT_UP = "weak_breakout_up"
    WEAK_BREAKOUT_DOWN = "weak_breakout_down"
    STRONG_BREAKOUT_UP = "strong_breakout_up"
    STRONG_BREAKOUT_DOWN = "strong_breakout_down"


@dataclass
class TrendRead:
    trend: Trend
    range_high: float
    range_low: float
    breakout_size: float          # signed, in price points, beyond the range
    breakout_frac_of_expected_move: float


def classify_from_bars(open_price: float, bars_high: float, bars_low: float,
                        last_price: float, expected_move: float) -> TrendRead:
    """bars_high/low = high/low over the opening-range window."""
    if last_price > bars_high:
        breakout_size = last_price - bars_high
    elif last_price < bars_low:
        breakout_size = last_price - bars_low  # negative
    else:
        breakout_size = 0.0

    frac = abs(breakout_size) / expected_move if expected_move > 0 else 0.0

    if breakout_size == 0.0:
        trend = Trend.RANGE_BOUND
    elif breakout_size > 0:
        trend = Trend.STRONG_BREAKOUT_UP if frac >= config.BREAKOUT_CONFIRM_FRACTION else Trend.WEAK_BREAKOUT_UP
    else:
        trend = Trend.STRONG_BREAKOUT_DOWN if frac >= config.BREAKOUT_CONFIRM_FRACTION else Trend.WEAK_BREAKOUT_DOWN

    return TrendRead(trend=trend, range_high=bars_high, range_low=bars_low,
                      breakout_size=breakout_size, breakout_frac_of_expected_move=frac)


def classify_from_daily_bar_proxy(prior_close: float, today_open: float,
                                   today_high: float, today_low: float,
                                   today_close: float, expected_move: float) -> TrendRead:
    """
    Coarse backtest-only proxy when only daily OHLC is available.

    IMPORTANT (lookahead bias): this MUST classify using only information
    known at/before the day's open -- i.e. the overnight gap (today_open vs
    prior_close). It deliberately does NOT look at today_high/today_low/
    today_close to make the classification, because those only exist after
    the decision would have been made; using them here would mean the
    backtest picks the correct side after already knowing the day's outcome.
    today_high/today_low/today_close are accepted only so callers have one
    consistent signature, and are NOT used below.

    This is a materially weaker signal than a true 30-min opening-range read
    (which does get to see live intraday price action before deciding) --
    expect backtest performance on this proxy to understate what a real
    opening-range classifier could do, not overstate it. See README section 6.
    """
    gap = today_open - prior_close
    frac = abs(gap) / expected_move if expected_move > 0 else 0.0

    if frac < config.BREAKOUT_CONFIRM_FRACTION * 0.5:
        trend = Trend.RANGE_BOUND
    elif gap > 0:
        trend = Trend.STRONG_BREAKOUT_UP if frac >= config.BREAKOUT_CONFIRM_FRACTION else Trend.WEAK_BREAKOUT_UP
    else:
        trend = Trend.STRONG_BREAKOUT_DOWN if frac >= config.BREAKOUT_CONFIRM_FRACTION else Trend.WEAK_BREAKOUT_DOWN

    return TrendRead(trend=trend, range_high=today_open, range_low=today_open,
                      breakout_size=gap, breakout_frac_of_expected_move=frac)
