"""
Tradier multi-leg order placement/closing for the sandbox account (per your
choice: auto-trade in Tradier SANDBOX only, not production).

Tradier's multileg order endpoint takes up to 4 legs in one call, exactly
what an Iron Condor/Iron Fly/skewed IIC needs (call spread + put spread).
This module builds those payloads from a `Recommendation`/strikes dict and
from a `position_manager.Lot`; it does not decide *whether* to trade --
that's strategy_selector.py and position_manager.py's job.

IMPORTANT: untested against a live Tradier sandbox account from this
delivery -- this sandbox environment has no network path to Tradier. Test
against a real Tradier sandbox account before trusting it, and double check
option symbol formatting (OCC format, e.g. SPXW260908C05000000) against
Tradier's own contract lookup before relying on the symbol builder below.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass

from modules.data_sources import TradierClient


def occ_symbol(underlying: str, expiration: dt.date, strike: float, is_call: bool) -> str:
    """Builds an OCC-format option symbol, e.g. SPXW260908C05000000.
    Verify this against Tradier's /markets/options/lookup for your account
    before relying on it -- SPX weeklies use the SPXW root, standard
    monthlies use SPX; get this wrong and orders will be rejected, which is
    the safe failure mode, but confirm rather than assume.
    """
    yy = expiration.strftime("%y%m%d")
    cp = "C" if is_call else "P"
    strike_int = int(round(strike * 1000))
    return f"{underlying}{yy}{cp}{strike_int:08d}"


@dataclass
class LegOrder:
    symbol: str
    side: str          # "sell_to_open" / "buy_to_close" etc.
    quantity: int
    option_symbol: str


def build_entry_legs(strikes: dict, expiration: dt.date, contracts: int,
                      underlying: str = "SPXW") -> list[dict]:
    """Sell the short legs, buy the long legs -- standard credit-spread
    opening. Returns Tradier multileg `leg` dicts."""
    legs = []
    if "call_short" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["call_short"], True),
                      "side": "sell_to_open", "quantity": contracts})
    if "call_long" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["call_long"], True),
                      "side": "buy_to_open", "quantity": contracts})
    if "put_short" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["put_short"], False),
                      "side": "sell_to_open", "quantity": contracts})
    if "put_long" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["put_long"], False),
                      "side": "buy_to_open", "quantity": contracts})
    return legs


def build_exit_legs(strikes: dict, expiration: dt.date, contracts: int,
                     underlying: str = "SPXW") -> list[dict]:
    """Reverse of build_entry_legs -- buy back what was sold, sell what was
    bought, to flatten the position."""
    legs = []
    if "call_short" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["call_short"], True),
                      "side": "buy_to_close", "quantity": contracts})
    if "call_long" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["call_long"], True),
                      "side": "sell_to_close", "quantity": contracts})
    if "put_short" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["put_short"], False),
                      "side": "buy_to_close", "quantity": contracts})
    if "put_long" in strikes:
        legs.append({"option_symbol": occ_symbol(underlying, expiration, strikes["put_long"], False),
                      "side": "sell_to_close", "quantity": contracts})
    return legs


def submit_multileg_order(client: TradierClient, account_id: str, underlying: str,
                           legs: list[dict], order_type: str = "market",
                           duration: str = "day", price: float | None = None) -> dict:
    """Submits a multileg order to the Tradier SANDBOX account. `account_id`
    is your Tradier sandbox account number (not the production one) -- get
    it from Tradier's /user/profile endpoint or the sandbox dashboard.
    A limit order (order_type="credit"/"debit" with `price`) is strongly
    preferred over "market" for multileg SPX spreads in practice; market
    multileg fills can be poor. Left as "market" here for simplicity --
    tighten this before trusting it with real fills."""
    import requests  # local import: this function is the one live-network path

    payload = {"class": "multileg", "symbol": underlying, "type": order_type,
               "duration": duration}
    if price is not None:
        payload["price"] = price
    for i, leg in enumerate(legs):
        payload[f"option_symbol[{i}]"] = leg["option_symbol"]
        payload[f"side[{i}]"] = leg["side"]
        payload[f"quantity[{i}]"] = leg["quantity"]

    r = requests.post(f"{client.base_url}/accounts/{account_id}/orders",
                       data=payload, headers=client._headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def get_spread_debit_to_close(client: TradierClient, strikes: dict, expiration: dt.date,
                               underlying: str = "SPXW") -> float:
    """Quotes each leg and returns the net debit (per contract) to close the
    position right now -- what position_manager.check_lot_exit needs as
    `current_debit_to_close_per_contract`. Uses mid price per leg; real
    fills will differ (see option_pricer.py's fill-haircut discussion --
    the same real-vs-theoretical gap applies to closing quotes, not just
    opening ones)."""
    symbols = []
    signs = []  # +1 = we are buying to close (pay), -1 = selling to close (receive)
    if "call_short" in strikes:
        symbols.append(occ_symbol(underlying, expiration, strikes["call_short"], True)); signs.append(+1)
    if "call_long" in strikes:
        symbols.append(occ_symbol(underlying, expiration, strikes["call_long"], True)); signs.append(-1)
    if "put_short" in strikes:
        symbols.append(occ_symbol(underlying, expiration, strikes["put_short"], False)); signs.append(+1)
    if "put_long" in strikes:
        symbols.append(occ_symbol(underlying, expiration, strikes["put_long"], False)); signs.append(-1)

    quote = client.get_quote(",".join(symbols))
    quotes = quote["quotes"]["quote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    by_symbol = {q["symbol"]: q for q in quotes}

    net = 0.0
    for sym, sign in zip(symbols, signs):
        q = by_symbol[sym]
        mid = (float(q["bid"]) + float(q["ask"])) / 2.0
        net += sign * mid
    return max(0.0, net)
