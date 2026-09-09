"""
Black-Scholes 0DTE option pricer with a realistic fill haircut.

0DTE options violate several BS assumptions (discrete jumps, wide bid/ask,
pinning effects near close) -- this is a *reference* price, not a fill
guarantee. The haircut factor is where we admit that and correct for it.
"""
from __future__ import annotations
import math
from dataclasses import dataclass

from scipy.stats import norm

import config


def _d1_d2(s: float, k: float, t: float, sigma: float, r: float):
    if t <= 0 or sigma <= 0:
        return None, None
    d1 = (math.log(s / k) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2


def bs_price(s: float, k: float, t_years: float, sigma: float, r: float, is_call: bool) -> float:
    """Black-Scholes theoretical mid price. t_years can be a tiny fraction (0DTE)."""
    if t_years <= 0:
        return max(0.0, (s - k) if is_call else (k - s))
    d1, d2 = _d1_d2(s, k, t_years, sigma, r)
    if d1 is None:
        return max(0.0, (s - k) if is_call else (k - s))
    if is_call:
        return s * norm.cdf(d1) - k * math.exp(-r * t_years) * norm.cdf(d2)
    return k * math.exp(-r * t_years) * norm.cdf(-d2) - s * norm.cdf(-d1)


@dataclass
class LegQuote:
    strike: float
    is_call: bool
    theoretical: float
    realistic_credit_or_debit: float  # after haircut, sign-aware for a short leg


def price_short_leg(s: float, k: float, t_years: float, sigma: float, r: float,
                     is_call: bool, haircut: float = config.FILL_HAIRCUT) -> LegQuote:
    """Price a short (sold) leg: theoretical BS mid, and the realistic credit
    we actually expect to collect after applying the fill haircut."""
    theo = bs_price(s, k, t_years, sigma, r, is_call)
    realistic = theo * haircut
    return LegQuote(strike=k, is_call=is_call, theoretical=theo,
                     realistic_credit_or_debit=realistic)


def expected_move(s: float, vix: float, dte_fraction_of_year: float) -> float:
    """1-sigma expected move for the remaining session using VIX as proxy IV."""
    sigma = vix / 100.0
    return s * sigma * math.sqrt(max(dte_fraction_of_year, 1e-6))
