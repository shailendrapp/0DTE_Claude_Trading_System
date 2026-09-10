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
    if not r.ok:
        # Same fix as modules/data_sources.py's _get(): print the response
        # body before raising, since raise_for_status() alone discards it.
        # A live run's first 500 here (2026-09-09) gave zero information
        # beyond "500 Server Error" -- root-caused separately to un-rounded
        # strikes producing an OCC symbol for a nonexistent contract (see
        # strategy_selector.build_strikes' SPX_STRIKE_INCREMENT fix), but
        # this print is what would have shown that directly instead of
        # requiring a guess.
        print(f"[Tradier {r.status_code}] POST /accounts/{account_id}/orders payload={payload}\n"
              f"Response body: {r.text}")
        r.raise_for_status()
    return r.json()


def get_order_status(client: TradierClient, account_id: str, order_id) -> dict:
    """GET /accounts/{account_id}/orders/{order_id} -- used to poll a
    just-submitted limit order for fill confirmation (see run_live.py's
    poll_for_fill()). UNTESTED against a live response for the exact
    field names Tradier uses for a filled multileg order's average price
    (this assumes `order.avg_fill_price`, per Tradier's documented order
    object shape) -- if that field is actually named something else or
    nested differently, poll_for_fill() will just never see it as
    filled and fall back to the pre-trade credit estimate, which is a
    safe degrade, not a crash. Confirm the real field name against your
    own account's response and adjust if needed."""
    import requests
    r = requests.get(f"{client.base_url}/accounts/{account_id}/orders/{order_id}",
                      headers=client._headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def cancel_order(client: TradierClient, account_id: str, order_id) -> dict:
    """DELETE /accounts/{account_id}/orders/{order_id} -- cancels a still-
    open (unfilled) order. Used by run_monitor.py when an entry limit order
    never got a confirmed fill and it's time to give up on it (hard EOD, or
    an explicit reconciliation pass) rather than leave a stale day-order
    sitting in the account indefinitely. Tradier no-ops harmlessly if the
    order already filled/cancelled/expired by the time this runs -- treat
    a non-ok response as informational, not fatal, since the caller's next
    step (marking the lot cancelled_unfilled) is correct either way."""
    import requests
    r = requests.delete(f"{client.base_url}/accounts/{account_id}/orders/{order_id}",
                         headers=client._headers(), timeout=10)
    if not r.ok:
        print(f"[Tradier {r.status_code}] DELETE /accounts/{account_id}/orders/{order_id}\n"
              f"Response body: {r.text}")
    return {"status_code": r.status_code, "ok": r.ok, "body": r.text}


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


def get_spread_credit_to_open(client: TradierClient, strikes: dict, expiration: dt.date,
                               underlying: str = "SPXW") -> float:
    """Real-market-quote equivalent of get_spread_debit_to_close(), but for
    the OPENING side. Quotes each leg and returns the net credit (per
    contract) actually available to open the position right now.

    BUG HISTORY (2026-09-10): run_live.py used to set the entry limit price
    off theoretical_net_credit() (Black-Scholes), then apply a flat
    config.FILL_HAIRCUT to approximate the real/theoretical gap. That's
    fine for near-the-money structures, but for a wide, deep-OTM 0DTE
    structure (a real trade that day: SPX iron condor, 85pt wings) it was
    catastrophically wrong -- BS priced the tail risk at $12.79 theoretical
    (-> $6.40 after the 0.5 haircut), while the real market (confirmed via
    an OptionStrat screenshot AND the Tradier sandbox order sitting
    unfilled all day at that price) was quoting a combined $0.45 credit for
    the same strikes. The gap isn't a fixed percentage -- it grows sharply
    with strike distance, so no single FILL_HAIRCUT constant can correct
    for it across all structures. The fix: stop guessing from BS and ask
    Tradier what the market actually is, exactly like get_spread_debit_to_
    close() already does for exits. Mid price per leg, same as that
    function -- real fills will differ (usually worse, since mid isn't
    guaranteed), which is exactly why ENTRY_LIMIT_PRICE_FRACTION still
    applies on top of whatever this returns, same as before.

    Sign convention (opposite of get_spread_debit_to_close, since these are
    the entry-side legs): +1 for sell_to_open legs (short strikes, credit
    received), -1 for buy_to_open legs (long strikes, debit paid).
    """
    symbols = []
    signs = []
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
