"""
Telegram alerting -- same pattern as Vajra's alerts. Uses Telegram's plain
HTTP bot API directly (no extra dependency beyond `requests`).

Setup (one-time, outside this code):
  1. Message @BotFather on Telegram, /newbot, get a bot token.
  2. Message your new bot once, then hit
     https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id.
  3. Set env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""
from __future__ import annotations
import os

try:
    import requests
except ImportError:
    requests = None


def _config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


ALERT_PREFIX = "*Claude* 🤖"


def send(message: str) -> bool:
    """Returns True on success, False if not configured or the send failed.
    Never raises -- an alert failure should not crash the trading loop.

    Every outbound alert is prefixed with ALERT_PREFIX so it's immediately
    identifiable in a chat that may also get messages from other sources.
    This is the single place that prefix is applied -- every format_*
    helper below stays prefix-free and just returns its own body."""
    token, chat_id = _config()
    prefixed = f"{ALERT_PREFIX}\n{message}"
    if not token or not chat_id or requests is None:
        print(f"[telegram disabled/not configured] {prefixed}")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": prefixed, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"[telegram send failed] {e}: {prefixed}")
        return False


def format_context_alert(window_name: str, ms, vs, score) -> str:
    """Matches the style you sketched: Trend/Volatility/Event risk/Expected
    move/Opening structure/Gamma resistance/Range probability/Bullish
    probability. Sent as a separate message right before the entry alert
    (or the no-trade alert) so the reasoning is visible even on a
    no-trade day. See scoring.py's docstring: these are transparent
    heuristic scores, not a validated statistical forecast -- worth
    remembering every time this prints "probability."
    """
    gamma_line = f"{vs.gamma_wall:.0f}" if vs.gamma_wall else "n/a (no usable OI-weighted chain data)"
    lines = [
        f"*0DTE Market Read — {window_name}*",
        f"Trend: {score.trend_score:+d} {score.trend_label}",
        f"Volatility: {score.volatility_regime}",
        f"Event risk: {score.event_risk_label}",
        f"Expected move: ±{vs.expected_move:.0f}",
        f"Opening structure: {ms.price_vs_vwap} VWAP, {ms.range_acceptance}",
        f"Gamma resistance/support: {gamma_line}",
        f"Range probability: {score.range_probability_pct:.0f}%",
        f"Bullish probability: {score.bullish_probability_pct:.0f}%",
    ]
    return "\n".join(lines)


def format_entry_alert(signal) -> str:
    lines = [f"*0DTE Entry — {signal.date}*", f"Structure: `{signal.structure}`",
             f"Strikes: `{signal.strikes}`", ""]
    for lot in signal.lots:
        target = f"{lot.profit_target_fraction:.0%} target" if lot.profit_target_fraction else "runner (ratcheting stop)"
        lines.append(f"Lot {lot.lot_index} — {lot.contracts}x @ ${lot.entry_credit_per_contract:.2f} credit — {target}")
    lines.append(f"\nHard EOD close: {signal.hard_eod_exit_et} ET")
    return "\n".join(lines)


def format_exit_alert(signal, lot, pnl: float) -> str:
    return (f"*0DTE Exit — {signal.date}*\n"
            f"Lot {lot.lot_index} ({lot.contracts}x) closed: `{lot.exit_reason}`\n"
            f"Exit price: ${lot.exit_price_per_contract:.2f}/contract\n"
            f"P&L: ${pnl:,.2f}")


def format_eod_summary(signal, summary: dict) -> str:
    lines = [f"*0DTE Day Summary — {signal.date}*", f"Total P&L: ${summary['total_pnl']:,.2f}", ""]
    for l in summary["lots"]:
        lines.append(f"Lot {l['lot']}: {l['status']} ({l['exit_reason']}) -> ${l['pnl']:,.2f}")
    return "\n".join(lines)


def format_period_summary(summary: dict) -> str:
    """summary is a dict from modules/pnl_log.py's daily_summary/
    weekly_summary/monthly_summary. label already says which period and
    date range this covers (e.g. "Daily Summary — 2026-09-09" or "Weekly
    Summary — 2026-09-07 to 2026-09-11")."""
    if summary["n_signals"] == 0:
        return f"*{summary['label']}*\nNo signals closed in this period."
    lines = [
        f"*{summary['label']}*",
        f"Signals: {summary['n_signals']} across {summary['n_days']} day(s), {summary['n_lots']} lots",
        f"Win rate: {summary['win_rate_pct']}% ({summary['wins']}W / {summary['losses']}L)",
        f"Total P&L: ${summary['total_pnl']:,.2f}",
    ]
    return "\n".join(lines)
