#!/usr/bin/env python3
"""
CLI: entry decision + order submission (Tradier SANDBOX) + Telegram alert,
for ONE entry window. Run this once per window, near/after that window's
range closes -- e.g. as 3 separate GitHub Actions cron-triggered steps
(09:45, 11:45, 13:45 ET) matching config.ENTRY_WINDOWS. Each run is
independent and stacks with any earlier windows' still-open positions
(per your choice: fixed time windows, stacking allowed, no automatic cap
unless you set config.MAX_CONCURRENT_SIGNALS).

Usage:
    python run_live.py --window morning
    python run_live.py --window midday
    python run_live.py --window afternoon

Trigger each window's cron AT OR AFTER its range closes (range_start_et +
range_minutes in config.ENTRY_WINDOWS), never before -- running early asks
Tradier for bars that don't exist yet and silently truncates the opening-
range read. With current config that's:
  morning:   09:30 + 30min -> trigger at/after 10:00 ET
  midday:    11:30 + 15min -> trigger at/after 11:45 ET
  afternoon: 13:30 + 15min -> trigger at/after 13:45 ET
(An earlier version of this docstring said 09:45 for morning -- that was
wrong for OPENING_RANGE_MINUTES=30 and has been corrected here.)

Required env vars:
  TRADIER_TOKEN, TRADIER_SANDBOX_ACCOUNT_ID
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (optional -- alerts just log if unset)

This sandbox has no network path to Tradier or Telegram, so this has not
been run end-to-end here -- test against your real Tradier sandbox account
before trusting it, starting with 1 contract.
"""
from __future__ import annotations
import argparse
import datetime as dt
import glob
import json
import os
import zoneinfo

import config
from modules.data_sources import TradierClient, parse_tradier_chain
from modules.market_open import classify_from_bars
from modules.iv_regime import classify as classify_iv
from modules.econ_calendar import check_event, should_delay_entry, FOMC_DATES_2026, CPI_DATES_2026, PPI_DATES_2026, NFP_DATES_2026
from modules.news_geopolitical import fetch_headlines, score_risk
from modules.strategy_selector import select_structure, build_strikes, Structure
from modules.option_pricer import expected_move, bs_price
from modules.position_manager import build_daily_signal
from modules.tradier_orders import build_entry_legs, submit_multileg_order
from modules.market_structure import build_market_structure
from modules.vol_structure import build_vol_structure
from modules.scoring import compute_composite_score
from modules import telegram_alerts

ET = zoneinfo.ZoneInfo("America/New_York")
STATE_DIR = "state"


def theoretical_net_credit(strikes: dict, spot: float, vix: float) -> float:
    sigma = vix / 100.0
    r = config.RISK_FREE_RATE
    t = 1.0 / config.TRADING_DAYS_PER_YEAR
    credit = 0.0
    if "call_short" in strikes and "call_long" in strikes:
        credit += bs_price(spot, strikes["call_short"], t, sigma, r, True) - bs_price(spot, strikes["call_long"], t, sigma, r, True)
    if "put_short" in strikes and "put_long" in strikes:
        credit += bs_price(spot, strikes["put_short"], t, sigma, r, False) - bs_price(spot, strikes["put_long"], t, sigma, r, False)
    return max(0.0, credit)


def get_window(name: str) -> dict:
    for w in config.ENTRY_WINDOWS:
        if w["name"] == name:
            return w
    raise SystemExit(f"Unknown window '{name}'. Valid: {[w['name'] for w in config.ENTRY_WINDOWS]}")


def count_open_signals_today(today: dt.date) -> int:
    """For MAX_CONCURRENT_SIGNALS enforcement -- counts state files for
    today that still have any OPEN lot."""
    from modules.position_manager import DailySignal, LotStatus
    count = 0
    for path in glob.glob(os.path.join(STATE_DIR, f"{today}_*.json")):
        with open(path) as f:
            sig = DailySignal.from_json(f.read())
        if any(lot.status == LotStatus.OPEN for lot in sig.lots):
            count += 1
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True, help="Entry window name, e.g. morning/midday/afternoon (see config.ENTRY_WINDOWS)")
    args = ap.parse_args()
    window = get_window(args.window)

    token = os.environ.get("TRADIER_TOKEN")
    account_id = os.environ.get("TRADIER_SANDBOX_ACCOUNT_ID")
    if not token or not account_id:
        raise SystemExit("Set TRADIER_TOKEN and TRADIER_SANDBOX_ACCOUNT_ID first.")

    client = TradierClient(token=token, sandbox=True)  # sandbox only, per design decision

    now_et = dt.datetime.now(ET)
    today = now_et.date()

    if config.MAX_CONCURRENT_SIGNALS is not None:
        open_count = count_open_signals_today(today)
        if open_count >= config.MAX_CONCURRENT_SIGNALS:
            msg = (f"*0DTE — {today} [{window['name']}]*: skipped, "
                   f"{open_count} signal(s) already open (MAX_CONCURRENT_SIGNALS={config.MAX_CONCURRENT_SIGNALS}).")
            telegram_alerts.send(msg)
            print(msg)
            return

    quote = client.get_quote("SPX")
    spot = float(quote["quotes"]["quote"]["last"])
    vix_quote = client.get_quote("VIX")
    vix = float(vix_quote["quotes"]["quote"]["last"])

    h, m = (int(x) for x in window["range_start_et"].split(":"))
    range_start = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    range_end = range_start + dt.timedelta(minutes=window["range_minutes"])
    bars = client.get_history("SPX", "1min", range_start.strftime("%Y-%m-%d %H:%M"),
                              range_end.strftime("%Y-%m-%d %H:%M"))
    day_bars = bars.get("history", {}).get("day", [])
    if not day_bars:
        raise SystemExit(f"No bars returned for window '{window['name']}' "
                          f"({range_start}-{range_end}) -- check market is open.")
    highs = [float(b["high"]) for b in day_bars]
    lows = [float(b["low"]) for b in day_bars]
    em = expected_move(spot, vix, 1.0 / config.TRADING_DAYS_PER_YEAR)
    trend_read = classify_from_bars(spot, max(highs), min(lows), spot, em)

    iv_regime = classify_iv(vix)
    event_flag = check_event(now_et, fomc_dates=FOMC_DATES_2026, cpi_dates=CPI_DATES_2026,
                              ppi_dates=PPI_DATES_2026, nfp_dates=NFP_DATES_2026)
    headlines = fetch_headlines()
    news_risk = score_risk(headlines, overnight_futures_gap_pct=0.0, overnight_vix_change_pct=0.0)  # wire real gap/vix-change inputs

    # ---- Market structure / vol structure / composite score, for the Telegram context alert ----
    daily_hist = client.get_history("SPX", "daily",
                                     (today - dt.timedelta(days=40)).isoformat(), today.isoformat())
    daily_bars_raw = daily_hist.get("history", {}).get("day", [])
    daily_bars = [{"high": float(b["high"]), "low": float(b["low"]), "close": float(b["close"])}
                  for b in daily_bars_raw]
    daily_closes = [b["close"] for b in daily_bars] + [spot]  # include today's running price
    prior_bar = daily_bars[-1] if daily_bars else {"high": spot, "low": spot, "close": spot}

    vix_hist = client.get_history("VIX", "daily",
                                   (today - dt.timedelta(days=5)).isoformat(), today.isoformat())
    vix_bars_raw = vix_hist.get("history", {}).get("day", [])
    vix_prior_close = float(vix_bars_raw[-1]["close"]) if vix_bars_raw else vix

    opening_range_bars = [{"high": float(b["high"]), "low": float(b["low"]),
                           "close": float(b["close"]), "volume": float(b.get("volume", 0) or 0)}
                          for b in day_bars]

    ms = build_market_structure(
        prior_close=prior_bar["close"], today_open=opening_range_bars[0]["close"] if opening_range_bars else spot,
        current_price=spot, opening_range_bars=opening_range_bars, daily_closes_incl_today=daily_closes,
        daily_bars_for_atr=daily_bars, prior_high=prior_bar["high"], prior_low=prior_bar["low"],
    )

    chain = []
    try:
        raw_chain = client.get_option_chain("SPXW", today.isoformat())
        chain = parse_tradier_chain(raw_chain)
    except Exception as e:  # noqa: BLE001
        print(f"[warning] couldn't fetch/parse option chain for gamma/skew context: {e}")

    from modules.market_structure import realized_vol_annualized
    rv = realized_vol_annualized(daily_closes)
    vs = build_vol_structure(vix=vix, vix_prior_close=vix_prior_close, spot=spot,
                              expected_move=em, realized_vol=rv, chain=chain)

    score = compute_composite_score(ms, vs, breakout_frac=trend_read.breakout_frac_of_expected_move,
                                     event_is_blocking=should_delay_entry(event_flag),
                                     news_score=news_risk.score)

    telegram_alerts.send(telegram_alerts.format_context_alert(window["name"], ms, vs, score))

    rec = select_structure(trend_read, iv_regime, event_flag, news_risk)

    tag = f"{today}_{window['name']}"
    if rec.structure == Structure.NO_TRADE:
        telegram_alerts.send(f"*0DTE — {tag}*: No trade. Reasons: {'; '.join(rec.reasons)}")
        print(f"No trade [{window['name']}]:", rec.reasons)
        return

    strikes = build_strikes(rec, spot, em, trend_read)
    total_contracts = max(config.NUM_LOTS, round(config.BASE_CONTRACTS * config.NUM_LOTS * rec.size_multiplier))
    theo_credit = theoretical_net_credit(strikes, spot, vix)
    entry_credit_per_contract = theo_credit * config.FILL_HAIRCUT  # expected, NOT guaranteed -- see actual fill via order response

    expiration = today  # 0DTE

    signal = build_daily_signal(tag, rec.structure.value, strikes,
                                 total_contracts, entry_credit_per_contract)

    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, f"{tag}.json")

    order_results = []
    for lot in signal.lots:
        if lot.contracts <= 0:
            continue
        lot_legs = build_entry_legs(strikes, expiration, contracts=lot.contracts)
        result = submit_multileg_order(client, account_id, "SPXW", lot_legs, order_type="market")
        order_results.append({"lot": lot.lot_index, "order": result})

    with open(state_path, "w") as f:
        f.write(signal.to_json())

    telegram_alerts.send(f"[{window['name']}]\n" + telegram_alerts.format_entry_alert(signal))
    print(f"[{window['name']}] Entered {rec.structure.value}, state saved to {state_path}")
    print(json.dumps(order_results, indent=2))


if __name__ == "__main__":
    main()
