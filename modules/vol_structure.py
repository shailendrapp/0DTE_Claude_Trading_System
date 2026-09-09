"""
Options/volatility structure features: VIX level & change, IV vs realized
vol, skew, a retail-style gamma-exposure ("gamma wall") estimate, distance
to expected-move boundaries, and 0DTE ATM delta/premium.

BE HONEST ABOUT THIS MODULE'S LIMITS, every time it's used:
- "Gamma exposure by strike" here is the standard retail GEX approximation
  (open interest x contract gamma x spot^2 x a sign convention assuming
  dealers are short calls/long puts against customer positioning). It is
  NOT real dealer positioning data -- nobody outside the market makers
  actually knows that. Treat the resulting "gamma wall" as a plausible
  support/resistance heuristic, not a fact. Real GEX providers (SpotGamma,
  SqueezeMetrics, etc.) use proprietary refinements this simple version
  does not attempt.
- Skew here is a simple 25-delta-ish proxy from whatever strikes are
  closest to 0.25/-0.25 delta in the chain, not a smoothed vol surface.
"""
from __future__ import annotations
from dataclasses import dataclass
import math


@dataclass
class VolStructure:
    vix: float
    vix_change_pct: float
    expected_move: float
    atm_iv: float | None
    iv_vs_rv: float | None            # atm_iv / realized_vol, >1 means IV rich vs realized
    skew_25d: float | None            # put_iv - call_iv at ~25-delta, positive = put skew (typical)
    gamma_wall: float | None          # strike with largest |gamma exposure| near spot
    gamma_exposure_by_strike: dict
    distance_to_upper_move_pct: float  # % of expected move already used up, upside
    distance_to_lower_move_pct: float


def vix_change_pct(vix_now: float, vix_prior_close: float) -> float:
    if vix_prior_close == 0:
        return 0.0
    return (vix_now - vix_prior_close) / vix_prior_close * 100.0


def nearest_by_delta(chain: list[dict], target_abs_delta: float, is_call: bool) -> dict | None:
    """chain: list of {strike, option_type, delta, iv, open_interest, gamma}.
    Returns the contract closest to the target absolute delta on the
    requested side, or None if the chain has nothing usable."""
    candidates = [c for c in chain if c.get("option_type") == ("call" if is_call else "put")
                  and c.get("delta") is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(abs(c["delta"]) - target_abs_delta))


def atm_iv_from_chain(chain: list[dict], spot: float) -> float | None:
    usable = [c for c in chain if c.get("iv") is not None and c.get("strike") is not None]
    if not usable:
        return None
    nearest = min(usable, key=lambda c: abs(c["strike"] - spot))
    return nearest["iv"]


def skew_25d(chain: list[dict]) -> float | None:
    put25 = nearest_by_delta(chain, 0.25, is_call=False)
    call25 = nearest_by_delta(chain, 0.25, is_call=True)
    if not put25 or not call25 or put25.get("iv") is None or call25.get("iv") is None:
        return None
    return put25["iv"] - call25["iv"]


def gamma_exposure_by_strike(chain: list[dict], spot: float, contract_multiplier: int = 100) -> dict:
    """Retail-style GEX approximation. Sign convention: dealers assumed net
    short calls / long puts against customer flow (the common simplifying
    assumption), so call OI contributes negative dealer gamma exposure and
    put OI contributes positive -- meaning large POSITIVE exposure near a
    strike is read as a support/pinning zone, large NEGATIVE as amplifying
    (dealers need to sell into declines / buy into rallies there). This is
    the standard simplification, not a claim about any specific dealer's
    actual book."""
    exposure: dict = {}
    for c in chain:
        strike = c.get("strike")
        gamma = c.get("gamma")
        oi = c.get("open_interest")
        if strike is None or gamma is None or not oi:
            continue
        sign = -1.0 if c.get("option_type") == "call" else 1.0
        contrib = sign * gamma * oi * contract_multiplier * (spot ** 2) * 0.01
        exposure[strike] = exposure.get(strike, 0.0) + contrib
    return exposure


def find_gamma_wall(exposure_by_strike: dict, spot: float, within_pct: float = 3.0) -> float | None:
    """Largest-magnitude exposure strike within `within_pct`% of spot --
    far-away strikes with huge OI (e.g. a stale far-OTM hedge) shouldn't be
    reported as "the" wall for a same-day 0DTE decision."""
    if not exposure_by_strike:
        return None
    band = spot * within_pct / 100.0
    nearby = {k: v for k, v in exposure_by_strike.items() if abs(k - spot) <= band}
    if not nearby:
        return None
    return max(nearby, key=lambda k: abs(nearby[k]))


def build_vol_structure(vix: float, vix_prior_close: float, spot: float, expected_move: float,
                         realized_vol: float | None, chain: list[dict] | None) -> VolStructure:
    chain = chain or []
    atm_iv = atm_iv_from_chain(chain, spot)
    iv_vs_rv = (atm_iv / realized_vol) if (atm_iv and realized_vol and realized_vol > 0) else None
    exposure = gamma_exposure_by_strike(chain, spot)
    wall = find_gamma_wall(exposure, spot)

    return VolStructure(
        vix=vix, vix_change_pct=vix_change_pct(vix, vix_prior_close),
        expected_move=expected_move, atm_iv=atm_iv, iv_vs_rv=iv_vs_rv,
        skew_25d=skew_25d(chain), gamma_wall=wall, gamma_exposure_by_strike=exposure,
        distance_to_upper_move_pct=0.0, distance_to_lower_move_pct=0.0,  # filled in relative to current price by caller
    )


def distance_to_move_boundaries(current_price: float, open_price: float, expected_move: float) -> tuple[float, float]:
    """% of the expected move already 'used up' in each direction since the
    open -- how close price is to the +/-1 expected-move boundary."""
    moved = current_price - open_price
    upper_pct = max(0.0, moved) / expected_move * 100.0 if expected_move > 0 else 0.0
    lower_pct = max(0.0, -moved) / expected_move * 100.0 if expected_move > 0 else 0.0
    return upper_pct, lower_pct
