#!/usr/bin/env python3
"""
CLI: position monitor for ALL of today's open signals (there can be more
than one now -- up to one per entry window in config.ENTRY_WINDOWS, and
they stack rather than replace each other). Run this frequently during
market hours, e.g. every ~5 min via a GitHub Actions cron trigger.

KNOWN LIMITATION, discussed and accepted: GitHub Actions cron does not
reliably guarantee sub-5-minute schedules. A fast intraday move can go
against a position for up to that ~5 minutes before this next runs and
closes it. If that gap ever proves too wide in practice, the fix is a
persistent worker polling every config.MONITOR_POLL_SECONDS instead of
repeated Actions runs -- not a change to this script's logic.

Loads every state/<date>_<window>.json for today that still has an open
lot, checks each lot's exit condition via position_manager.check_lot_exit,
closes any that should close (Tradier sandbox order + Telegram alert), and
rewrites the state file. Fully-closed signals are renamed to .done so they
stop being re-processed (and so the EOD summary is sent exactly once).

Required env vars: same as run_live.py.
"""
from __future__ import annotations
import datetime as dt
import glob
import os
import zoneinfo

import config
from modules.data_sources import TradierClient
from modules.position_manager import (DailySignal, check_lot_exit, apply_exit, lot_pnl, signal_summary,
                                       LotStatus, past_hard_eod, is_terminal)
from modules.tradier_orders import (build_exit_legs, submit_multileg_order, get_spread_debit_to_close,
                                     get_order_status, cancel_order)
from modules import telegram_alerts, pnl_log

ET = zoneinfo.ZoneInfo("America/New_York")
STATE_DIR = "state"


def open_state_paths_today(today: dt.date) -> list[str]:
    return sorted(glob.glob(os.path.join(STATE_DIR, f"{today}_*.json")))


def reconcile_pending_lot(client: TradierClient, account_id: str, signal: DailySignal,
                           lot, now_et: dt.datetime) -> bool:
    """A PENDING_FILL lot's entry order never confirmed filled within
    run_live.py's short poll window (see that module's BUG HISTORY comment,
    2026-09-10). This is where it eventually gets resolved -- checked every
    monitor run until either a real fill shows up or hard EOD arrives.
    Returns True if the lot's status changed (caller should persist state).

    - No order_id at all (submission itself didn't return one) -> nothing
      to track or cancel; treat as never having existed.
    - Order now shows filled -> promote to OPEN with the real fill price
      (or, if the status says filled but no price field is found, still
      promote to OPEN using the pre-trade estimate rather than leave a
      real position untracked -- log it loudly either way).
    - Order rejected/canceled/expired on Tradier's side already -> mark
      cancelled, nothing to do.
    - Still genuinely pending and hard EOD has passed -> actively cancel
      the day order (don't just let it silently expire unseen) and mark
      cancelled_unfilled.
    - Still pending and still before hard EOD -> leave it, try again next
      run (a limit order may yet fill later in the session)."""
    if lot.order_id is None:
        print(f"[{signal.date}] Lot {lot.lot_index} has no order_id to reconcile -- "
              f"treating as never-entered.")
        lot.status = LotStatus.CANCELLED_UNFILLED
        lot.exit_reason = "no_order_id"
        lot.exit_time = now_et.isoformat()
        return True

    try:
        status = get_order_status(client, account_id, lot.order_id)
        order = status.get("order", status)
        state = (order.get("status") or "").lower()
    except Exception as e:  # noqa: BLE001
        print(f"[{signal.date}] Lot {lot.lot_index}: couldn't poll order {lot.order_id} status "
              f"({e}) -- leaving PENDING_FILL, will retry next run.")
        return False

    if state == "filled":
        fill = order.get("avg_fill_price")
        if fill is not None:
            lot.entry_credit_per_contract = float(fill)
        else:
            print(f"[{signal.date}] Lot {lot.lot_index}: order {lot.order_id} shows filled but no "
                  f"avg_fill_price -- promoting to OPEN with the pre-trade estimate anyway "
                  f"(a real position exists and MUST be tracked for exit, even with an imperfect credit number).")
        lot.status = LotStatus.OPEN
        print(f"[{signal.date}] Lot {lot.lot_index}: order {lot.order_id} confirmed filled "
              f"(late) at ${lot.entry_credit_per_contract:.2f}/contract -- now OPEN.")
        return True

    if state in ("rejected", "canceled", "cancelled", "expired"):
        lot.status = LotStatus.CANCELLED_UNFILLED
        lot.exit_reason = f"order_{state}"
        lot.exit_time = now_et.isoformat()
        print(f"[{signal.date}] Lot {lot.lot_index}: order {lot.order_id} ended '{state}' -- "
              f"marking cancelled_unfilled.")
        return True

    # Still genuinely open/pending on Tradier's side.
    if past_hard_eod(signal, now_et):
        cancel_result = cancel_order(client, account_id, lot.order_id)
        lot.status = LotStatus.CANCELLED_UNFILLED
        lot.exit_reason = "never_filled_cancelled_at_eod"
        lot.exit_time = now_et.isoformat()
        print(f"[{signal.date}] Lot {lot.lot_index}: order {lot.order_id} never filled and hard "
              f"EOD has passed -- cancelled (result: {cancel_result}), marking cancelled_unfilled.")
        return True

    return False  # still pending, still before hard EOD -- leave it, retry next run


def process_one(client: TradierClient, account_id: str, state_path: str, now_et: dt.datetime) -> None:
    with open(state_path) as f:
        signal = DailySignal.from_json(f.read())

    if all(is_terminal(lot) for lot in signal.lots):
        # Shouldn't normally happen (should already be .done), but be safe.
        os.rename(state_path, state_path.replace(".json", ".done"))
        return

    any_change = False

    # Reconcile any PENDING_FILL lots FIRST, in the same run -- a lot that
    # gets promoted to OPEN here (a late real fill) is then immediately
    # eligible for the normal exit-condition check right below, in this
    # same pass, rather than waiting an extra ~5min monitor cycle.
    for lot in signal.lots:
        if lot.status == LotStatus.PENDING_FILL:
            if reconcile_pending_lot(client, account_id, signal, lot, now_et):
                any_change = True

    expiration = now_et.date()  # 0DTE
    underlying_price = float(client.get_quote("SPX")["quotes"]["quote"]["last"])
    debit_to_close = get_spread_debit_to_close(client, signal.strikes, expiration)

    for lot in signal.lots:
        if lot.status != LotStatus.OPEN:
            continue
        reason = check_lot_exit(signal, lot, debit_to_close, underlying_price, now_et)
        if reason is None:
            continue

        exit_legs = build_exit_legs(signal.strikes, expiration, contracts=lot.contracts)
        # Only a profit-target exit is discretionary enough to risk a
        # limit order -- stop-loss/strike-test/hard-EOD exits need
        # certainty of getting out NOW more than they need a good price,
        # so those three always stay "market" regardless of config.
        if reason == "profit_target":
            limit_price = round(debit_to_close * config.EXIT_LIMIT_PRICE_FRACTION, 2)
            submit_multileg_order(client, account_id, "SPXW", exit_legs,
                                   order_type=config.EXIT_LIMIT_ORDER_TYPE, price=limit_price)
        else:
            submit_multileg_order(client, account_id, "SPXW", exit_legs, order_type="market")
        apply_exit(lot, reason, debit_to_close, now_et)
        any_change = True

        pnl = lot_pnl(lot)
        telegram_alerts.send(telegram_alerts.format_exit_alert(signal, lot, pnl))
        print(f"[{signal.date}] Closed lot {lot.lot_index}: {reason}, pnl={pnl:.2f}")

    if any_change:
        with open(state_path, "w") as f:
            f.write(signal.to_json())

    # BUG HISTORY (2026-09-10): this used to be `!= OPEN`, which treated a
    # still-unresolved PENDING_FILL lot as "done" the moment it existed
    # (since PENDING_FILL != OPEN too) -- prematurely sending the EOD
    # summary/renaming to .done while an entry order was genuinely still
    # working. is_terminal() correctly excludes PENDING_FILL, so a signal
    # only wraps up once every lot is truly resolved one way or another.
    if all(is_terminal(lot) for lot in signal.lots):
        summary = signal_summary(signal)
        telegram_alerts.send(telegram_alerts.format_eod_summary(signal, summary))
        # signal.date is "<YYYY-MM-DD>_<window_name>" (see run_live.py's
        # `tag`); window names have no underscores so rsplit(1) is safe.
        date_str, window_name = signal.date.rsplit("_", 1)
        pnl_log.append_signal(date_str, window_name, signal.structure,
                               summary["total_pnl"], len(signal.lots))
        print(f"[{signal.date}] Day complete:", summary)
        os.rename(state_path, state_path.replace(".json", ".done"))


def maybe_send_period_summaries(today: dt.date) -> None:
    """Called only on the run that closes the LAST still-open signal for
    today (see main() below), so this fires at most once per day -- no
    open_state_paths_today() left to re-trigger it on later runs. Daily
    fires every day something closed; weekly/monthly are additionally
    gated by modules/pnl_log.py's calendar-approximate triggers (see that
    module's docstring for the market-holiday caveat)."""
    rows = pnl_log.load_log()
    telegram_alerts.send(telegram_alerts.format_period_summary(pnl_log.daily_summary(rows, today)))
    if pnl_log.is_last_weekday_of_week(today):
        telegram_alerts.send(telegram_alerts.format_period_summary(pnl_log.weekly_summary(rows, today)))
    if pnl_log.is_last_trading_day_of_month(today):
        telegram_alerts.send(telegram_alerts.format_period_summary(pnl_log.monthly_summary(rows, today)))


def main():
    token = os.environ.get("TRADIER_TOKEN")
    account_id = os.environ.get("TRADIER_SANDBOX_ACCOUNT_ID")
    if not token or not account_id:
        raise SystemExit("Set TRADIER_TOKEN and TRADIER_SANDBOX_ACCOUNT_ID first.")

    client = TradierClient(token=token, sandbox=True)
    now_et = dt.datetime.now(ET)
    today = now_et.date()

    paths = open_state_paths_today(today)
    if not paths:
        print("No open signals for today (none taken yet, or all already closed).")
        return

    for state_path in paths:
        process_one(client, account_id, state_path, now_et)

    if not open_state_paths_today(today):
        maybe_send_period_summaries(today)


if __name__ == "__main__":
    main()
