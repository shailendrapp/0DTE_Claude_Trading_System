"""
Scale-out position management for a single daily 0DTE signal split into
config.NUM_LOTS lots (default 3: 50% target / 70% target / runner).

This module is pure decision logic -- no Tradier or Telegram calls here,
so it can be unit-tested and reasoned about on its own. tradier_orders.py
and telegram_alerts.py act on what this module decides.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Optional
import json

import config


class LotStatus(str, Enum):
    # BUG HISTORY (2026-09-10): a limit-order entry that never got a
    # confirmed fill was still recorded with the default OPEN status and
    # the pre-trade theoretical credit -- a phantom position. run_monitor.py
    # would then try to "exit" it at hard EOD, which (since no real
    # position exists) would actually have opened a brand-new, unintended
    # position in the opposite direction rather than closing anything.
    # Root cause surfaced by a real trade (2026-09-10 morning): the limit
    # price was ~28x the real market credit for that wide/deep-OTM
    # structure (a Black-Scholes-vs-real-market gap far beyond what
    # config.FILL_HAIRCUT corrects for), so it sat open all day. PENDING_
    # FILL is a new, explicit "not yet a real position" state distinct
    # from OPEN, so it's never treated as something to exit; CANCELLED_
    # UNFILLED is its terminal resting state once run_monitor.py gives up
    # on it (see run_monitor.py's reconcile_pending_lot()).
    PENDING_FILL = "pending_fill"
    OPEN = "open"
    CLOSED_PROFIT_TARGET = "closed_profit_target"
    CLOSED_STOP_LOSS = "closed_stop_loss"
    CLOSED_STRIKE_TEST = "closed_strike_test"
    CLOSED_EOD = "closed_eod"
    CANCELLED_UNFILLED = "cancelled_unfilled"


@dataclass
class Lot:
    lot_index: int                 # 0-based; last index is the runner if its target is None
    contracts: int
    entry_credit_per_contract: float   # realistic (haircut-applied) credit received at entry
    profit_target_fraction: Optional[float]  # None => runner, no fixed target
    status: LotStatus = LotStatus.OPEN
    stop_multiple: float = config.STOP_LOSS_CREDIT_MULTIPLE
    exit_price_per_contract: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_time: Optional[str] = None
    order_id: Optional[int] = None  # the entry order's Tradier id -- needed to
    # re-poll a PENDING_FILL lot's status later, or cancel it, in run_monitor.py

    @property
    def is_runner(self) -> bool:
        return self.profit_target_fraction is None


@dataclass
class DailySignal:
    date: str
    structure: str
    strikes: dict
    lots: list  # list[Lot]
    hard_eod_exit_et: str = config.HARD_EOD_EXIT_ET

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, default=str)

    @staticmethod
    def from_json(s: str) -> "DailySignal":
        d = json.loads(s)
        d["lots"] = [Lot(**{**lot, "status": LotStatus(lot["status"])}) for lot in d["lots"]]
        return DailySignal(**d)


def build_daily_signal(date: str, structure: str, strikes: dict,
                        total_contracts: int, entry_credit_per_contract: float) -> DailySignal:
    """Split one signal into config.NUM_LOTS lots. If total_contracts isn't
    evenly divisible, earlier lots get the remainder (so the runner is never
    larger than the profit-target lots)."""
    n = config.NUM_LOTS
    base = total_contracts // n
    remainder = total_contracts % n
    lots = []
    for i in range(n):
        contracts = base + (1 if i < remainder else 0)
        target = config.LOT_PROFIT_TARGETS[i] if i < len(config.LOT_PROFIT_TARGETS) else None
        lots.append(Lot(lot_index=i, contracts=contracts,
                         entry_credit_per_contract=entry_credit_per_contract,
                         profit_target_fraction=target))
    return DailySignal(date=date, structure=structure, strikes=strikes, lots=lots)


def _closed_profit_target_count(signal: DailySignal, up_to_index: int) -> int:
    return sum(1 for lot in signal.lots
               if lot.lot_index < up_to_index
               and lot.status == LotStatus.CLOSED_PROFIT_TARGET)


def current_stop_level(signal: DailySignal, lot: Lot) -> float:
    """Returns the debit-to-close level that triggers a stop for this lot,
    per contract. Non-runner lots always use the flat stop multiple. The
    runner's stop ratchets down as earlier lots bank their profit target."""
    if not lot.is_runner:
        return lot.entry_credit_per_contract * lot.stop_multiple

    closed_before_runner = sum(
        1 for other in signal.lots
        if other.lot_index != lot.lot_index and other.status == LotStatus.CLOSED_PROFIT_TARGET
    )
    if closed_before_runner >= config.RUNNER_LOCK_AFTER_LOTS_CLOSED:
        # lock in RUNNER_LOCK_FRACTION of the runner's own credit
        return lot.entry_credit_per_contract * (1 - config.RUNNER_LOCK_FRACTION)
    if closed_before_runner >= config.RUNNER_BREAKEVEN_AFTER_LOTS_CLOSED:
        # breakeven: don't pay more to close than was collected
        return lot.entry_credit_per_contract
    return lot.entry_credit_per_contract * lot.stop_multiple


def _within_market_hours_before_hard_exit(now_et: datetime, hard_exit_str: str) -> bool:
    h, m = (int(x) for x in hard_exit_str.split(":"))
    return now_et.time() < dt_time(h, m)


def past_hard_eod(signal: DailySignal, now_et: datetime) -> bool:
    """Public wrapper so callers outside this module (run_monitor.py's
    PENDING_FILL reconciliation) can reuse the exact same hard-EOD check
    check_lot_exit uses internally, instead of re-deriving it."""
    return not _within_market_hours_before_hard_exit(now_et, signal.hard_eod_exit_et)


# Lot statuses that mean "not yet resolved, still needs monitor.py attention"
# -- OPEN (a real filled position, needs exit-condition checks) and
# PENDING_FILL (an entry order that hasn't been confirmed filled yet, needs
# reconciliation -- see run_monitor.py's reconcile_pending_lot()). Anything
# else (CLOSED_*, CANCELLED_UNFILLED) is terminal for that lot.
ACTIVE_STATUSES = (LotStatus.OPEN, LotStatus.PENDING_FILL)


def is_terminal(lot: Lot) -> bool:
    return lot.status not in ACTIVE_STATUSES


def check_lot_exit(signal: DailySignal, lot: Lot, current_debit_to_close_per_contract: float,
                    underlying_price: float, now_et: datetime) -> Optional[str]:
    """
    Returns an exit reason string if this OPEN lot should be closed now, else
    None. Caller (tradier_orders.py) is responsible for actually placing the
    closing order and then calling apply_exit().
    """
    if lot.status != LotStatus.OPEN:
        return None

    # Hard time-based exit always wins, regardless of P&L.
    if not _within_market_hours_before_hard_exit(now_et, signal.hard_eod_exit_et):
        return "eod_close"

    entry_credit = lot.entry_credit_per_contract

    # Profit target (lots 0..n-2 typically; runner has none).
    if lot.profit_target_fraction is not None:
        captured_fraction = 1.0 - (current_debit_to_close_per_contract / entry_credit) if entry_credit > 0 else 0.0
        if captured_fraction >= lot.profit_target_fraction:
            return "profit_target"

    # Stop-loss (ratcheted for the runner).
    stop_level = current_stop_level(signal, lot)
    if current_debit_to_close_per_contract >= stop_level:
        return "stop_loss"

    # Defensive strike-test exit, runner only.
    if lot.is_runner and config.RUNNER_EXIT_ON_STRIKE_TEST:
        nearest_short_strikes = [v for k, v in signal.strikes.items() if k.endswith("_short")]
        for strike in nearest_short_strikes:
            # "tested" = underlying has traded through the short strike
            # (i.e. the spread is now ITM on that side).
            is_call_side = any(k == "call_short" and v == strike for k, v in signal.strikes.items())
            if is_call_side and underlying_price >= strike:
                return "strike_tested"
            if not is_call_side and underlying_price <= strike:
                return "strike_tested"

    return None


def apply_exit(lot: Lot, reason: str, exit_price_per_contract: float, now_et: datetime) -> None:
    status_map = {
        "profit_target": LotStatus.CLOSED_PROFIT_TARGET,
        "stop_loss": LotStatus.CLOSED_STOP_LOSS,
        "strike_tested": LotStatus.CLOSED_STRIKE_TEST,
        "eod_close": LotStatus.CLOSED_EOD,
    }
    lot.status = status_map[reason]
    lot.exit_price_per_contract = exit_price_per_contract
    lot.exit_reason = reason
    lot.exit_time = now_et.isoformat()


def lot_pnl(lot: Lot, multiplier: int = 100) -> float:
    if lot.exit_price_per_contract is None:
        return 0.0
    return (lot.entry_credit_per_contract - lot.exit_price_per_contract) * multiplier * lot.contracts


def signal_summary(signal: DailySignal) -> dict:
    total_pnl = sum(lot_pnl(lot) for lot in signal.lots)
    return {
        "date": signal.date,
        "structure": signal.structure,
        "total_pnl": round(total_pnl, 2),
        "lots": [
            {"lot": lot.lot_index, "contracts": lot.contracts, "status": lot.status.value,
             "exit_reason": lot.exit_reason, "pnl": round(lot_pnl(lot), 2)}
            for lot in signal.lots
        ],
    }
