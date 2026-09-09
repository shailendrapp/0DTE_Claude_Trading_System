"""
Intraday, multi-window, stacking backtest -- the version that actually
models your "2-3 trades/day, stacking, scale-out lots with ratcheting
stop" design, as opposed to backtest_engine.py's single-signal,
hold-to-close model.

Requires a per-day simulated intraday path (modules/intraday_path.py) since
no real intraday history was available for the full 10-year window -- see
that module's docstring for exactly what assumption that introduces.
Everything downstream of the path (trend read, event/news filters,
structure selection, lot-level exit logic) is the SAME code the live
system (run_live.py/run_monitor.py) uses -- this backtest is not a
separate reimplementation of the rules, it replays the real decision path
through a simulated market.
"""
from __future__ import annotations
from dataclasses import asdict
import datetime as dt
import math
import zoneinfo

import pandas as pd

import config
from modules.intraday_path import simulate_intraday_path, price_at_minute, local_high_low, SESSION_MINUTES
from modules.market_open import classify_from_bars
from modules.iv_regime import classify as classify_iv
from modules.econ_calendar import check_event
from modules.news_geopolitical import NewsRiskResult
from modules.strategy_selector import select_structure, build_strikes, Structure
from modules.option_pricer import bs_price, expected_move
from modules.position_manager import build_daily_signal, check_lot_exit, apply_exit, lot_pnl, LotStatus

ET = zoneinfo.ZoneInfo("America/New_York")
SPX_MULTIPLIER = 100
HARD_EOD_MINUTE = 15 * 60 + 45 - (9 * 60 + 30)  # 15:45 ET relative to 9:30 open = 375


def _window_minutes(window: dict) -> tuple[int, int]:
    h, m = (int(x) for x in window["range_start_et"].split(":"))
    start_minute = (h * 60 + m) - (9 * 60 + 30)
    return start_minute, start_minute + window["range_minutes"]


def _remaining_t_years(minute: int) -> float:
    remaining_minutes = max(SESSION_MINUTES - minute, 1)
    return (remaining_minutes / SESSION_MINUTES) * (1.0 / config.TRADING_DAYS_PER_YEAR)


def _net_theo_credit(strikes: dict, spot: float, t_years: float, sigma: float, r: float) -> float:
    credit = 0.0
    if "call_short" in strikes and "call_long" in strikes:
        credit += bs_price(spot, strikes["call_short"], t_years, sigma, r, True) - bs_price(spot, strikes["call_long"], t_years, sigma, r, True)
    if "put_short" in strikes and "put_long" in strikes:
        credit += bs_price(spot, strikes["put_short"], t_years, sigma, r, False) - bs_price(spot, strikes["put_long"], t_years, sigma, r, False)
    return max(0.0, credit)


def run_intraday_backtest(df: pd.DataFrame, fomc_dates=None, cpi_dates=None, ppi_dates=None,
                           nfp_dates=None, contracts_base: int = config.BASE_CONTRACTS,
                           step_minutes: int = 5) -> pd.DataFrame:
    """
    df: date, open, high, low, close, vix (same shape as backtest_engine.py).
    Returns one row per LOT (not per day) -- a day with 3 windows x 3 lots
    can contribute up to 9 rows, fewer on days a window skips (news/event/
    extreme-vol gate) or MAX_CONCURRENT_SIGNALS caps it.
    """
    fomc_dates, cpi_dates = fomc_dates or [], cpi_dates or []
    ppi_dates, nfp_dates = ppi_dates or [], nfp_dates or []

    rows = []
    for _, row in df.iterrows():
        d = row["date"].date() if hasattr(row["date"], "date") else row["date"]
        vix = float(row["vix"])
        sigma = vix / 100.0
        r = config.RISK_FREE_RATE
        open_p, high_p, low_p, close_p = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])

        path = simulate_intraday_path(d, open_p, high_p, low_p, close_p, step_minutes=step_minutes)

        open_signals = []  # list of (signal, ) still being walked forward this day

        for window in config.ENTRY_WINDOWS:
            if config.MAX_CONCURRENT_SIGNALS is not None and len(open_signals) >= config.MAX_CONCURRENT_SIGNALS:
                continue

            start_m, decision_m = _window_minutes(window)
            spot = price_at_minute(path, decision_m)
            # BUG this smoke test caught before trusting the 10yr run: the
            # range window must NOT include the decision minute itself --
            # local_high_low(path, start_m, decision_m) would make `spot`
            # (priced at that same decision_m) structurally guaranteed to
            # sit inside [range_low, range_high], since it's one of the
            # points defining that max/min. Every window then classified as
            # range-bound by construction, no matter what the path did --
            # confirmed by a first full run that produced ZERO breakout
            # trades across 22,611 lots, 100% Balanced IC. Fixed by
            # measuring the range up to (but excluding) the decision
            # minute, so `spot` -- one step later -- can actually fall
            # outside it.
            range_high, range_low = local_high_low(path, start_m, max(start_m, decision_m - step_minutes))

            t_years = _remaining_t_years(decision_m)
            em = expected_move(spot, vix, t_years)
            trend_read = classify_from_bars(price_at_minute(path, start_m), range_high, range_low, spot, em)

            iv_regime = classify_iv(vix)
            decision_dt = dt.datetime.combine(
                d, dt.time((9 * 60 + 30 + decision_m) // 60, (9 * 60 + 30 + decision_m) % 60), tzinfo=ET)
            event_flag = check_event(decision_dt, fomc_dates, cpi_dates, ppi_dates, nfp_dates)
            news_risk = NewsRiskResult(score=0, matched_headlines=[], matched_keywords=[],
                                        market_confirms=False, note="Backtest: no live news feed, assumed neutral.")

            rec = select_structure(trend_read, iv_regime, event_flag, news_risk)
            if rec.structure == Structure.NO_TRADE:
                continue

            strikes = build_strikes(rec, spot, em, trend_read)
            total_contracts = max(config.NUM_LOTS, round(contracts_base * config.NUM_LOTS * rec.size_multiplier))
            theo_credit = _net_theo_credit(strikes, spot, t_years, sigma, r)
            entry_credit = theo_credit * config.FILL_HAIRCUT

            signal = build_daily_signal(f"{d}_{window['name']}", rec.structure.value, strikes,
                                         total_contracts, entry_credit)
            open_signals.append((signal, decision_m))

        # Walk the path forward from each signal's own entry minute to EOD,
        # checking every open lot at each step.
        for signal, entry_m in open_signals:
            for m in range(entry_m, SESSION_MINUTES + 1, step_minutes):
                if all(lot.status != LotStatus.OPEN for lot in signal.lots):
                    break
                price = price_at_minute(path, m)
                t_rem = _remaining_t_years(m)
                # NOTE on two bugs caught by the tiny-sample smoke test
                # before trusting this on the real 10yr data, both from
                # unit-consistency mistakes between entry and exit pricing:
                #   1. First attempt divided the exit debit by
                #      FILL_HAIRCUT (stacking a second discount on top of
                #      the already-haircut entry credit) -- a zero-move,
                #      zero-time round trip looked like an instant ~4x-credit
                #      loss.
                #   2. Second attempt priced the exit at plain theoretical
                #      mid (no haircut at all). That's inconsistent with an
                #      entry credit recorded at haircut x theoretical: with
                #      STOP_LOSS_CREDIT_MULTIPLE=2.0 and FILL_HAIRCUT=0.5,
                #      2.0 x 0.5 = 1.0 exactly, so the stop level came out
                #      equal to the UN-haircut theoretical entry price with
                #      zero cushion -- every lot's stop tripped on literally
                #      the first check via the >= tie.
                # Fixed by pricing the exit in the SAME haircut-adjusted
                # units as the entry credit (multiply by FILL_HAIRCUT on
                # both sides, not divide on one and not skip it on the
                # other). At t=entry with no price movement this correctly
                # gives 0% profit captured and no stop trigger; as the day's
                # theta decay reduces theoretical value even with no
                # adverse move, the profit-target lots can and do close
                # profitably later in the session, as intended. This is a
                # simplification (a real bid/ask is not simply theoretical
                # mid x one constant in both directions) but it is at least
                # internally consistent, unlike the two attempts above --
                # a real exit-side slippage parameter would need actual
                # historical closing quotes to calibrate, which weren't
                # available for this delivery.
                debit_realistic = _net_theo_credit(signal.strikes, price, t_rem, vix / 100.0, config.RISK_FREE_RATE) * config.FILL_HAIRCUT

                now_et = dt.datetime.combine(
                    d, dt.time((9 * 60 + 30 + m) // 60, (9 * 60 + 30 + m) % 60), tzinfo=ET)

                for lot in signal.lots:
                    if lot.status != LotStatus.OPEN:
                        continue
                    reason = check_lot_exit(signal, lot, debit_realistic, price, now_et)
                    if reason:
                        apply_exit(lot, reason, debit_realistic, now_et)

            # anything still open at the very last step (shouldn't happen --
            # hard EOD check inside check_lot_exit should have caught it --
            # but force-close defensively at the day's real close price)
            for lot in signal.lots:
                if lot.status == LotStatus.OPEN:
                    debit_realistic = _net_theo_credit(signal.strikes, close_p, 1e-6, vix / 100.0, config.RISK_FREE_RATE) * config.FILL_HAIRCUT
                    apply_exit(lot, "eod_close", debit_realistic,
                               dt.datetime.combine(d, dt.time(15, 59), tzinfo=ET))

            for lot in signal.lots:
                rows.append({
                    "date": str(d), "window": signal.date.split("_")[-1], "structure": signal.structure,
                    "lot": lot.lot_index, "contracts": lot.contracts, "vix": vix,
                    "entry_credit": lot.entry_credit_per_contract, "exit_price": lot.exit_price_per_contract,
                    "exit_reason": lot.exit_reason, "pnl": lot_pnl(lot),
                })

    return pd.DataFrame(rows)


_WINDOW_ORDER = {w["name"]: i for i, w in enumerate(config.ENTRY_WINDOWS)}


def summarize_intraday(results: pd.DataFrame) -> dict:
    if results.empty:
        return {"n_lots": 0, "note": "No lots taken."}

    ordered = results.copy()
    ordered["window_order"] = ordered["window"].map(_WINDOW_ORDER).fillna(99)
    ordered = ordered.sort_values(["date", "window_order", "lot"])

    n_lots = len(ordered)
    wins = ordered[ordered["pnl"] > 0]
    losses = ordered[ordered["pnl"] <= 0]
    win_rate = len(wins) / n_lots
    total_pnl = ordered["pnl"].sum()
    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    equity = ordered["pnl"].cumsum()
    drawdown = equity - equity.cummax()

    exit_reason_counts = ordered["exit_reason"].value_counts().to_dict()

    return {
        "n_lots": n_lots,
        "n_signals": ordered.groupby(["date", "window"]).ngroups,
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win": round(wins["pnl"].mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(losses["pnl"].mean(), 2) if len(losses) else 0.0,
        "expectancy_per_lot": round(total_pnl / n_lots, 2),
        "total_pnl": round(total_pnl, 2),
        "profit_factor": round(profit_factor, 2) if math.isfinite(profit_factor) else "inf (no losing trades)",
        "max_drawdown": round(drawdown.min(), 2),
        "exit_reason_breakdown": exit_reason_counts,
        "structure_breakdown": ordered["structure"].value_counts().to_dict(),
        "window_breakdown": ordered.groupby("window")["pnl"].agg(["count", "sum", "mean"]).round(2).to_dict("index"),
    }
