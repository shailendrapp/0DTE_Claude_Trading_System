"""
Backtest engine: replays the strategy selector day-by-day over an SPX/VIX
price series (real, if supplied via CSV; synthetic demonstration otherwise)
and reports win rate, expectancy, average win/loss, profit factor, and max
drawdown -- not just win rate, per the README's warning about that metric
alone being meaningless for 0DTE credit strategies.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, time as dt_time
import math
import zoneinfo

import pandas as pd

import config
from modules.market_open import classify_from_daily_bar_proxy, Trend
from modules.iv_regime import classify as classify_iv
from modules.econ_calendar import check_event, EventType
from modules.news_geopolitical import NewsRiskResult
from modules.strategy_selector import select_structure, build_strikes, Structure
from modules.option_pricer import bs_price, expected_move

ET = zoneinfo.ZoneInfo("America/New_York")
SPX_MULTIPLIER = 100
DTE_YEARS = 1.0 / config.TRADING_DAYS_PER_YEAR  # 0DTE priced at start of day as "1 day to expiry"


@dataclass
class DayResult:
    date: str
    structure: str
    size_multiplier: float
    net_credit_realistic: float
    loss_at_expiry: float
    pnl: float
    trend: str
    vix: float
    event: str
    reasons: str


def _vertical_credit_and_loss(strikes: dict, close: float, spot: float, vix: float):
    """Returns (net_credit_theoretical, net_credit_realistic, loss_at_expiry)
    for whichever legs are present in `strikes`."""
    sigma = vix / 100.0
    r = config.RISK_FREE_RATE
    theo_credit = 0.0
    loss = 0.0

    if "call_short" in strikes and "call_long" in strikes:
        cs, cl = strikes["call_short"], strikes["call_long"]
        short_p = bs_price(spot, cs, DTE_YEARS, sigma, r, is_call=True)
        long_p = bs_price(spot, cl, DTE_YEARS, sigma, r, is_call=True)
        theo_credit += max(0.0, short_p - long_p)
        call_loss = max(0.0, close - cs) - max(0.0, close - cl)
        loss += min(max(call_loss, 0.0), cl - cs)

    if "put_short" in strikes and "put_long" in strikes:
        ps, pl = strikes["put_short"], strikes["put_long"]
        short_p = bs_price(spot, ps, DTE_YEARS, sigma, r, is_call=False)
        long_p = bs_price(spot, pl, DTE_YEARS, sigma, r, is_call=False)
        theo_credit += max(0.0, short_p - long_p)
        put_loss = max(0.0, ps - close) - max(0.0, pl - close)
        loss += min(max(put_loss, 0.0), ps - pl)

    realistic_credit = theo_credit * config.FILL_HAIRCUT
    return theo_credit, realistic_credit, loss


def run_backtest(df: pd.DataFrame, fomc_dates: list | None = None,
                  cpi_dates: list | None = None, contracts: int = config.BASE_CONTRACTS) -> pd.DataFrame:
    """
    df must have columns: date, open, high, low, close, vix (see
    data_sources.load_csv_series / synthetic_spx_vix_path).
    """
    fomc_dates = fomc_dates or []
    cpi_dates = cpi_dates or []
    results: list[DayResult] = []

    prior_close = None
    for _, row in df.iterrows():
        d = row["date"].date() if hasattr(row["date"], "date") else row["date"]
        vix = float(row["vix"])
        open_p, high_p, low_p, close_p = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

        if prior_close is None:
            prior_close = open_p

        em = expected_move(open_p, vix, DTE_YEARS)
        trend_read = classify_from_daily_bar_proxy(prior_close, open_p, high_p, low_p, close_p, em)
        iv_regime = classify_iv(vix)
        decision_dt = datetime.combine(d, dt_time(10, 0), tzinfo=ET)
        event_flag = check_event(decision_dt, fomc_dates, cpi_dates)
        # No real headline feed in the backtest; assume a neutral news day.
        news_risk = NewsRiskResult(score=0, matched_headlines=[], matched_keywords=[],
                                    market_confirms=False, note="Backtest: no live news feed, assumed neutral.")

        rec = select_structure(trend_read, iv_regime, event_flag, news_risk)

        if rec.structure == Structure.NO_TRADE:
            results.append(DayResult(str(d), rec.structure.value, 0.0, 0.0, 0.0, 0.0,
                                      trend_read.trend.value, vix, event_flag.event_type.value,
                                      "; ".join(rec.reasons)))
            prior_close = close_p
            continue

        strikes = build_strikes(rec, open_p, em, trend_read)
        _, realistic_credit, loss = _vertical_credit_and_loss(strikes, close_p, open_p, vix)

        size = rec.size_multiplier * contracts
        pnl = (realistic_credit - loss) * SPX_MULTIPLIER * size

        results.append(DayResult(str(d), rec.structure.value, rec.size_multiplier,
                                  realistic_credit, loss, pnl, trend_read.trend.value, vix,
                                  event_flag.event_type.value, "; ".join(rec.reasons)))
        prior_close = close_p

    return pd.DataFrame([asdict(r) for r in results])


def summarize(results: pd.DataFrame) -> dict:
    traded = results[results["structure"] != "no_trade"].copy()
    n_trades = len(traded)
    n_skipped = len(results) - n_trades

    if n_trades == 0:
        return {"n_trades": 0, "n_skipped": n_skipped, "note": "No trades taken."}

    wins = traded[traded["pnl"] > 0]
    losses = traded[traded["pnl"] <= 0]
    win_rate = len(wins) / n_trades
    avg_win = wins["pnl"].mean() if len(wins) else 0.0
    avg_loss = losses["pnl"].mean() if len(losses) else 0.0
    total_pnl = traded["pnl"].sum()
    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    expectancy_per_trade = total_pnl / n_trades

    equity = traded["pnl"].cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_drawdown = drawdown.min() if len(drawdown) else 0.0

    return {
        "n_trades": n_trades,
        "n_skipped": n_skipped,
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy_per_trade": round(expectancy_per_trade, 2),
        "total_pnl": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else "inf (no losing trades)",
        "max_drawdown": round(max_drawdown, 2),
        "structure_breakdown": traded["structure"].value_counts().to_dict(),
    }
