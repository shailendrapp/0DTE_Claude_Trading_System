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
from modules.position_manager import DailySignal, check_lot_exit, apply_exit, lot_pnl, signal_summary, LotStatus
from modules.tradier_orders import build_exit_legs, submit_multileg_order, get_spread_debit_to_close
from modules import telegram_alerts, pnl_log

ET = zoneinfo.ZoneInfo("America/New_York")
STATE_DIR = "state"


def open_state_paths_today(today: dt.date) -> list[str]:
    return sorted(glob.glob(os.path.join(STATE_DIR, f"{today}_*.json")))


def process_one(client: TradierClient, account_id: str, state_path: str, now_et: dt.datetime) -> None:
    with open(state_path) as f:
        signal = DailySignal.from_json(f.read())

    if all(lot.status != LotStatus.OPEN for lot in signal.lots):
        # Shouldn't normally happen (should already be .done), but be safe.
        os.rename(state_path, state_path.replace(".json", ".done"))
        return

    expiration = now_et.date()  # 0DTE
    underlying_price = float(client.get_quote("SPX")["quotes"]["quote"]["last"])
    debit_to_close = get_spread_debit_to_close(client, signal.strikes, expiration)

    any_change = False
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

    if all(lot.status != LotStatus.OPEN for lot in signal.lots):
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
