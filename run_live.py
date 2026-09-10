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
    python run_live.py --window morning --dry-run   # any time of day/week, see below

Trigger each window's cron AT OR AFTER its range closes (range_start_et +
range_minutes in config.ENTRY_WINDOWS), never before -- running early asks
Tradier for bars that don't exist yet and silently truncates the opening-
range read. With current config that's:
  morning:   09:30 + 30min -> trigger at/after 10:00 ET
  midday:    11:30 + 15min -> trigger at/after 11:45 ET
  afternoon: 13:30 + 15min -> trigger at/after 13:45 ET
(An earlier version of this docstring said 09:45 for morning -- that was
wrong for OPENING_RANGE_MINUTES=30 and has been corrected here.)

WHY A NORMAL RUN CAN'T WORK "irrespective of market hours": this asks
Tradier for real intraday bars for TODAY's window. If that window hasn't
happened yet (e.g. it's midnight and you're asking for 9:30-10:00 AM
bars), no data source on earth can return it -- it hasn't occurred. That
is not a bug in this script; it would be true of any system wired to a
real market data feed. --dry-run below is the actual fix for "I want to
be able to run/verify this any time": if today's window hasn't closed
yet, it transparently falls back to the most recent COMPLETED trading
day's data for that same window, runs the full read/decision pipeline
against it (structure selection, strikes, sizing, the Telegram context +
entry alert), and prints/sends it clearly marked "[DRY RUN]" -- but does
NOT submit any Tradier order and does NOT write a state/ file, so it can
never interfere with a real signal or pollute the P&L log. It proves the
pipeline is wired correctly end to end; it does not (and cannot) prove
what today's actual data will produce, because that doesn't exist yet.

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
import time
import zoneinfo

import config
from modules.data_sources import TradierClient, parse_tradier_chain
from modules.market_open import classify_from_bars
from modules.iv_regime import classify as classify_iv
from modules.econ_calendar import check_event, should_delay_entry, load_calendar_dates
from modules.news_geopolitical import fetch_headlines, score_news_risk
from modules.strategy_selector import select_structure, build_strikes, Structure
from modules.option_pricer import expected_move, bs_price
from modules.position_manager import build_daily_signal, LotStatus
from modules.tradier_orders import (build_entry_legs, submit_multileg_order, get_order_status,
                                     get_spread_credit_to_open)
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


def poll_for_fill(client: TradierClient, account_id: str, order_id, fallback_estimate: float) -> tuple[float, bool]:
    """Polls an order up to config.ORDER_FILL_POLL_ATTEMPTS times,
    config.ORDER_FILL_POLL_SECONDS apart, for fill confirmation. Returns
    (credit_per_contract, was_actually_filled). On any failure to
    confirm a fill within the window -- still pending, rejected, or the
    status response didn't have the expected shape -- returns
    `fallback_estimate` (the pre-trade theoretical*haircut estimate) so
    state/ always has SOME number to work with; the order itself is left
    alone (NOT cancelled) since duration="day" means it can still fill
    later even if we stop watching it here."""
    for attempt in range(config.ORDER_FILL_POLL_ATTEMPTS):
        try:
            status = get_order_status(client, account_id, order_id)
            order = status.get("order", status)
            state = (order.get("status") or "").lower()
            if state == "filled":
                fill = order.get("avg_fill_price")
                if fill is not None:
                    return float(fill), True
                print(f"[warning] order {order_id} shows filled but no avg_fill_price field -- "
                      f"using pre-trade estimate instead. Raw order object: {order}")
                return fallback_estimate, False
            if state in ("rejected", "canceled", "expired"):
                print(f"[warning] order {order_id} ended as '{state}', not filled -- "
                      f"using pre-trade estimate for this lot (no position exists for it).")
                return fallback_estimate, False
        except Exception as e:  # noqa: BLE001
            print(f"[warning] polling order {order_id} status failed (attempt {attempt+1}): {e}")
        if attempt < config.ORDER_FILL_POLL_ATTEMPTS - 1:
            time.sleep(config.ORDER_FILL_POLL_SECONDS)
    print(f"[warning] order {order_id} not confirmed filled after "
          f"{config.ORDER_FILL_POLL_ATTEMPTS * config.ORDER_FILL_POLL_SECONDS}s of polling -- "
          f"it may still fill later (duration=day); using pre-trade estimate for now. Check "
          f"Tradier directly and reconcile manually if needed.")
    return fallback_estimate, False


def most_recent_completed_weekday(before: dt.date) -> dt.date:
    """Approximation used only by --dry-run: the prior weekday, with no
    market-holiday calendar behind it (same caveat as pnl_log.py's
    is_last_trading_day_of_month). Good enough for "give me SOME real
    completed session to test against"; not a claim that it was actually
    a trading day."""
    d = before - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True, help="Entry window name, e.g. morning/midday/afternoon (see config.ENTRY_WINDOWS)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Exercise the full pipeline without submitting Tradier orders or writing "
                          "state/pnl_log. If today's window hasn't closed yet, falls back to the most "
                          "recent completed trading day so this can run any time. See module docstring.")
    args = ap.parse_args()
    window = get_window(args.window)

    # Startup heartbeat -- sent BEFORE any Tradier/env-var validation below,
    # so a manual "Run workflow" restart (or every scheduled fire, per your
    # choice -- this alert fires on ALL of them, not just manual triggers)
    # gets an immediate Telegram confirmation the engine actually started,
    # independent of whether anything downstream succeeds or fails. GITHUB_
    # EVENT_NAME is a default env var GitHub Actions sets on every step
    # (workflow_dispatch for a manual run, schedule for cron) -- no workflow
    # YAML change needed to see it here.
    trigger_source = os.environ.get("GITHUB_EVENT_NAME", "local/manual")
    startup_time = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z")
    startup_msg = (f"🟢 *0DTE Engine started* — window: {window['name']}, "
                   f"mode: {'DRY RUN' if args.dry_run else 'LIVE'}, trigger: {trigger_source}, "
                   f"{startup_time}")
    telegram_alerts.send(startup_msg)
    print(startup_msg)

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

    effective_date = today  # the date whose window we're actually reading
    if args.dry_run and now_et < range_end:
        fallback_date = most_recent_completed_weekday(today)
        print(f"[dry-run] {today}'s {window['name']} window closes at "
              f"{range_end.strftime('%H:%M %Z')} but it's currently {now_et.strftime('%H:%M %Z')} -- "
              f"using {fallback_date} instead so this can be exercised any time. "
              f"(No market-holiday calendar behind this -- see most_recent_completed_weekday().)")
        effective_date = fallback_date
        range_start = range_start.replace(year=fallback_date.year, month=fallback_date.month, day=fallback_date.day)
        range_end = range_end.replace(year=fallback_date.year, month=fallback_date.month, day=fallback_date.day)

    # BUG HISTORY (2026-09-09): the morning window got entered TWICE in one
    # day (a 7:15am run and a separate 10:26am run), and because both wrote
    # to the same state/<date>_<window>.json path, the second run silently
    # overwrote the first's state -- the first entry's runner lot (Lot 2)
    # was never tracked or closed by run_monitor.py again after that, and
    # its filled Tradier order was left open/unmanaged. This has nothing to
    # do with WHETHER a run was triggered manually or by cron -- the real
    # bug is that nothing stopped a second entry for the same window/day
    # regardless of trigger source. Guard it here: if a signal already
    # exists for this exact tag (open OR already closed/.done), skip
    # entirely rather than re-entering and clobbering it. --dry-run is
    # exempt on purpose -- it never writes state/, so it must stay safely
    # re-runnable any time (that's the whole point of --dry-run).
    tag = f"{effective_date}_{window['name']}"
    state_path = os.path.join(STATE_DIR, f"{tag}.json")
    if not args.dry_run:
        done_path = state_path.replace(".json", ".done")
        existing = state_path if os.path.exists(state_path) else (done_path if os.path.exists(done_path) else None)
        if existing:
            status_word = "still open, being monitored" if existing == state_path else "already closed for today"
            msg = (f"*0DTE — {tag}*: skipped -- a signal already exists for this window today "
                   f"({status_word}). Not re-entering (this would otherwise overwrite/duplicate it). "
                   f"If you meant to just exercise the pipeline, use --dry-run instead (never touches "
                   f"state/); if you deliberately want to redo this window's entry, remove/rename "
                   f"`{existing}` first.")
            telegram_alerts.send(msg)
            print(msg)
            return

    bars = client.get_timesales("SPX", "1min", range_start.strftime("%Y-%m-%d %H:%M"),
                                 range_end.strftime("%Y-%m-%d %H:%M"))
    day_bars = (bars.get("series") or {}).get("data", [])
    if isinstance(day_bars, dict):  # Tradier returns a bare dict, not a list, for a single bar
        day_bars = [day_bars]
    if not day_bars:
        reason = ("that date may not have been a real trading day (holiday/weekend edge case in "
                   "the no-calendar fallback above)" if args.dry_run else
                   "check market is open and this isn't being run before the window's range has "
                   "actually elapsed")
        raise SystemExit(f"No bars returned for window '{window['name']}' "
                          f"({range_start}-{range_end}) -- {reason}.")
    highs = [float(b["high"]) for b in day_bars]
    lows = [float(b["low"]) for b in day_bars]
    em = expected_move(spot, vix, 1.0 / config.TRADING_DAYS_PER_YEAR)
    trend_read = classify_from_bars(spot, max(highs), min(lows), spot, em)

    iv_regime = classify_iv(vix)
    # Prefers data/econ_calendar_cache.json (weekly-refreshed, covers
    # whichever years refresh_econ_calendar.py has fetched -- see
    # .github/workflows/calendar_refresh.yml); falls back to econ_calendar.
    # py's hardcoded 2026-only lists if that cache doesn't exist yet.
    cal = load_calendar_dates()
    event_flag = check_event(now_et, fomc_dates=cal["fomc"], cpi_dates=cal["cpi"],
                              ppi_dates=cal["ppi"], nfp_dates=cal["nfp"])
    headlines = fetch_headlines()
    news_risk = score_news_risk(headlines, overnight_futures_gap_pct=0.0, overnight_vix_change_pct=0.0)  # wire real gap/vix-change inputs

    # ---- Market structure / vol structure / composite score, for the Telegram context alert ----
    # Uses effective_date (== today, except in a --dry-run fallback) so the
    # daily lookback lines up with whichever session's intraday bars we
    # actually fetched above.
    daily_hist = client.get_history("SPX", "daily",
                                     (effective_date - dt.timedelta(days=40)).isoformat(), effective_date.isoformat())
    daily_bars_raw = daily_hist.get("history", {}).get("day", [])
    daily_bars = [{"high": float(b["high"]), "low": float(b["low"]), "close": float(b["close"])}
                  for b in daily_bars_raw]
    daily_closes = [b["close"] for b in daily_bars] + [spot]  # include today's running price
    prior_bar = daily_bars[-1] if daily_bars else {"high": spot, "low": spot, "close": spot}

    vix_hist = client.get_history("VIX", "daily",
                                   (effective_date - dt.timedelta(days=5)).isoformat(), effective_date.isoformat())
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
        raw_chain = client.get_option_chain("SPXW", effective_date.isoformat())
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

    dry_tag = "[DRY RUN] " if args.dry_run else ""
    context_msg = telegram_alerts.format_context_alert(window["name"], ms, vs, score)
    telegram_alerts.send(dry_tag + context_msg if args.dry_run else context_msg)

    rec = select_structure(trend_read, iv_regime, event_flag, news_risk)

    # tag/state_path were already established above (right after
    # effective_date), where the duplicate-entry guard lives.
    if rec.structure == Structure.NO_TRADE:
        telegram_alerts.send(f"{dry_tag}*0DTE — {tag}*: No trade. Reasons: {'; '.join(rec.reasons)}")
        print(f"No trade [{window['name']}]:", rec.reasons)
        return

    strikes = build_strikes(rec, spot, em, trend_read)
    total_contracts = max(config.NUM_LOTS, round(config.BASE_CONTRACTS * config.NUM_LOTS * rec.size_multiplier))
    expiration = effective_date  # 0DTE

    theo_credit = theoretical_net_credit(strikes, spot, vix)
    # BUG HISTORY (2026-09-10): this used to be the ONLY pricing basis --
    # entry_credit_per_contract and the limit price were both set off
    # theo_credit (Black-Scholes) * config.FILL_HAIRCUT. That's what caused
    # the 2026-09-10 morning entry to be priced ~28x the real market for a
    # wide/deep-OTM iron condor (BS said $12.79 theoretical / $6.40 after
    # haircut; the real market, confirmed via an OptionStrat screenshot AND
    # the Tradier sandbox order sitting unfilled all day, was $0.45 total).
    # A flat haircut can't correct for a gap that scales with strike
    # distance. Fix: ask Tradier for real bid/ask quotes on these exact
    # legs (get_spread_credit_to_open(), same pattern run_monitor.py
    # already uses on the closing side via get_spread_debit_to_close()) and
    # use THAT as the pricing basis instead. theo_credit is kept as a
    # fallback only -- if the live quote call fails outright, or the chain
    # returns something degenerate (<=0 credit, e.g. no quotes yet on an
    # illiquid leg), fall back to theoretical*haircut rather than trade on
    # a KeyError/exception, but log loudly either way so a fallback is
    # visible, not silent.
    try:
        market_credit = get_spread_credit_to_open(client, strikes, expiration)
    except Exception as e:  # noqa: BLE001
        print(f"[warning] couldn't fetch real market entry credit ({e}) -- falling back to "
              f"theoretical*haircut estimate (${theo_credit * config.FILL_HAIRCUT:.2f}).")
        market_credit = None

    if market_credit is None or market_credit <= 0.0:
        if market_credit is not None:  # fetched fine, just degenerate
            print(f"[warning] real-market entry credit quoted as ${market_credit:.2f} (<=0) -- "
                  f"falling back to theoretical*haircut estimate (${theo_credit * config.FILL_HAIRCUT:.2f}). "
                  f"This can happen if the chain is illiquid/has no quotes yet -- double check before "
                  f"trusting size on a fallback-priced entry.")
        pricing_credit = theo_credit * config.FILL_HAIRCUT
    else:
        pricing_credit = market_credit
        ratio = f"{market_credit / theo_credit:.1%}" if theo_credit > 0 else "n/a"
        print(f"[pricing] theoretical (BS) credit=${theo_credit:.2f}, real market credit=${market_credit:.2f} "
              f"(market is {ratio} of theoretical) -- using real market credit as the pricing basis.")

    entry_credit_per_contract = pricing_credit  # expected, NOT guaranteed -- see actual fill via order response

    signal = build_daily_signal(tag, rec.structure.value, strikes,
                                 total_contracts, entry_credit_per_contract)

    if args.dry_run:
        # No order, no state file, no pnl_log row -- this run proved the
        # pipeline computes a structure/strikes/sizing end to end; it must
        # never look like (or interfere with) a real signal.
        entry_msg = f"{dry_tag}[{window['name']}]\n" + telegram_alerts.format_entry_alert(signal)
        telegram_alerts.send(entry_msg)
        print(f"[dry-run][{window['name']}] Would have entered {rec.structure.value} "
              f"(effective_date={effective_date}) -- no order submitted, no state written.")
        print(json.dumps({"structure": rec.structure.value, "strikes": strikes,
                           "total_contracts": total_contracts,
                           "entry_credit_per_contract": round(entry_credit_per_contract, 2)}, indent=2))
        return

    os.makedirs(STATE_DIR, exist_ok=True)

    # BUG HISTORY: the first live order (2026-09-09) used order_type=
    # "market" -- flagged in tradier_orders.py itself as risky for a
    # multileg spread's fill quality. Now submits a LIMIT ("credit")
    # order at config.ENTRY_LIMIT_PRICE_FRACTION of the full theoretical
    # credit, then polls briefly for a real fill and, if confirmed,
    # overwrites the lot's pre-trade (haircut-estimated) credit with the
    # ACTUAL fill price -- so profit targets/stops downstream (position_
    # manager.py, all keyed off lot.entry_credit_per_contract) are
    # computed against reality, not a guess, whenever a fill is
    # confirmed in time. See poll_for_fill()'s docstring for the
    # fallback behavior when it isn't.
    # BUG HISTORY (2026-09-10): this used to be theo_credit (Black-Scholes)
    # -- now priced off pricing_credit (real market quote, falling back to
    # theoretical*haircut only if the quote call failed/degenerated -- see
    # above), so the limit order is grounded in what the market is actually
    # showing rather than a theoretical model that badly overprices wide/
    # deep-OTM 0DTE structures.
    limit_price = round(pricing_credit * config.ENTRY_LIMIT_PRICE_FRACTION, 2) \
        if config.ENTRY_ORDER_TYPE != "market" else None

    order_results = []
    for lot in signal.lots:
        if lot.contracts <= 0:
            continue
        lot_legs = build_entry_legs(strikes, expiration, contracts=lot.contracts)
        result = submit_multileg_order(client, account_id, "SPXW", lot_legs,
                                        order_type=config.ENTRY_ORDER_TYPE, price=limit_price)
        order_id = (result.get("order") or {}).get("id")
        lot.order_id = order_id  # needed for run_monitor.py to reconcile a still-pending fill later

        filled_credit, was_filled = (lot.entry_credit_per_contract, False)
        if order_id is not None:
            filled_credit, was_filled = poll_for_fill(client, account_id, order_id,
                                                        fallback_estimate=lot.entry_credit_per_contract)
        else:
            print(f"[warning] order submission response for lot {lot.lot_index} had no order id -- "
                  f"can't poll for fill, using pre-trade estimate. Raw response: {result}")

        # BUG HISTORY (2026-09-10): a lot that never got a confirmed fill
        # used to stay at its default status=OPEN with the pre-trade
        # theoretical credit recorded as if it were real -- a phantom
        # position. run_monitor.py would then try to "exit" it at hard
        # EOD, which (since nothing was actually opened) would have
        # submitted a brand-new position in the opposite direction rather
        # than closing anything. Root-caused to a real trade (2026-09-10
        # morning, a wide/deep-OTM iron condor) where the limit price was
        # ~28x the real market credit and so sat open, unfilled, all day.
        # Lots that don't confirm filled here are now PENDING_FILL, not
        # OPEN -- check_lot_exit() already skips anything that isn't OPEN,
        # and run_monitor.py separately reconciles PENDING_FILL lots (polls
        # for a late fill; cancels the order and marks it cancelled_unfilled
        # once hard EOD passes) rather than treating them as exitable.
        if was_filled:
            lot.entry_credit_per_contract = filled_credit
        else:
            lot.status = LotStatus.PENDING_FILL

        order_results.append({"lot": lot.lot_index, "order": result,
                               "limit_price": limit_price, "confirmed_filled": was_filled,
                               "status": lot.status.value,
                               "recorded_entry_credit_per_contract": lot.entry_credit_per_contract})

    with open(state_path, "w") as f:
        f.write(signal.to_json())

    # Built AFTER the fill-reconciliation loop above, so it reflects real
    # confirmed fill prices where we got them, not the pre-trade estimate.
    entry_msg = f"[{window['name']}]\n" + telegram_alerts.format_entry_alert(signal)
    telegram_alerts.send(entry_msg)
    print(f"[{window['name']}] Entered {rec.structure.value}, state saved to {state_path}")
    print(json.dumps(order_results, indent=2))


if __name__ == "__main__":
    main()
