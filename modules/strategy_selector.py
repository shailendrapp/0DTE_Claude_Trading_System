"""
Strategy decision matrix -- combines trend, event, news, and IV regime
into a recommended structure + strikes. This is the direct successor to
Vajra's single VIX-20 if/else; see README section 5 for the matrix in
prose form.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

import config
from modules.market_open import Trend, TrendRead
from modules.iv_regime import IVRegime
from modules.econ_calendar import EventFlag, should_delay_entry, size_multiplier
from modules.news_geopolitical import NewsRiskResult


class Structure(str, Enum):
    IRON_FLY = "iron_fly"
    BALANCED_IC = "balanced_iron_condor"
    SKEWED_IIC = "skewed_unbalanced_iron_condor"
    PUT_CREDIT_SPREAD = "put_credit_spread"     # sold when trend is up, downside protected
    CALL_CREDIT_SPREAD = "call_credit_spread"   # sold when trend is down, upside protected
    NO_TRADE = "no_trade"


@dataclass
class Recommendation:
    structure: Structure
    size_multiplier: float
    reasons: list[str] = field(default_factory=list)
    strikes: dict | None = None  # filled in by build_strikes()


def select_structure(trend_read: TrendRead, iv_regime: IVRegime,
                      event_flag: EventFlag, news_risk: NewsRiskResult) -> Recommendation:
    reasons: list[str] = []
    size = 1.0

    # --- Hard gates first ---
    if news_risk.score >= config.NEWS_RISK_SKIP_THRESHOLD:
        return Recommendation(Structure.NO_TRADE, 0.0,
                               [f"News/geopolitical risk score {news_risk.score}/3 -- skip day.", news_risk.note])

    if should_delay_entry(event_flag):
        return Recommendation(Structure.NO_TRADE, 0.0,
                               [f"{event_flag.event_type} inside delay window -- wait for post-release settle.",
                                event_flag.note])

    if iv_regime == IVRegime.EXTREME and trend_read.trend in (Trend.STRONG_BREAKOUT_UP, Trend.STRONG_BREAKOUT_DOWN):
        return Recommendation(Structure.NO_TRADE, 0.0,
                               ["VIX >= extreme breakpoint AND a strong breakout in progress -- "
                                "tail risk too high for a credit-selling structure today."])

    # --- Size adjustments ---
    if news_risk.score >= config.NEWS_RISK_HALF_SIZE_THRESHOLD:
        size *= 0.5
        reasons.append(f"News risk score {news_risk.score}/3 -- size halved.")
    size *= size_multiplier(event_flag)
    if event_flag.event_type.value == "OPEX":
        reasons.append("Monthly OPEX -- size reduced per econ calendar filter.")
    if iv_regime == IVRegime.EXTREME:
        size *= 0.5
        reasons.append("VIX in extreme regime -- size halved regardless of structure.")

    # --- Structure selection ---
    if trend_read.trend == Trend.RANGE_BOUND:
        use_fly = iv_regime == IVRegime.LOW and config.USE_IRON_FLY_IN_LOW_IV_RANGE_BOUND
        structure = Structure.IRON_FLY if use_fly else Structure.BALANCED_IC
        reasons.append(f"Range-bound open, IV regime={iv_regime.value} -> {structure.value}"
                        f"{' (Iron Fly disabled by config -- see backtest evidence)' if iv_regime == IVRegime.LOW and not use_fly else ''}.")
    elif trend_read.trend == Trend.WEAK_BREAKOUT_UP:
        structure = Structure.BALANCED_IC if iv_regime == IVRegime.LOW else Structure.SKEWED_IIC
        reasons.append(f"Weak breakout up, IV regime={iv_regime.value} -> {structure.value} "
                        "(put side widened / call side tightened).")
    elif trend_read.trend == Trend.WEAK_BREAKOUT_DOWN:
        structure = Structure.BALANCED_IC if iv_regime == IVRegime.LOW else Structure.SKEWED_IIC
        reasons.append(f"Weak breakout down, IV regime={iv_regime.value} -> {structure.value} "
                        "(call side widened / put side tightened).")
    elif trend_read.trend == Trend.STRONG_BREAKOUT_UP:
        structure = Structure.PUT_CREDIT_SPREAD
        reasons.append("Strong breakout up -- single-side put credit spread only, no call side sold.")
    else:  # STRONG_BREAKOUT_DOWN
        structure = Structure.CALL_CREDIT_SPREAD
        reasons.append("Strong breakout down -- single-side call credit spread only, no put side sold.")

    return Recommendation(structure=structure, size_multiplier=round(size, 3), reasons=reasons)


def build_strikes(rec: Recommendation, spot: float, expected_move: float,
                   trend_read: TrendRead, wing_width: float | None = None) -> dict:
    """
    Very simple strike-distance rule: short strikes at ~1x expected move on
    the "safe" side, tightened toward ~0.6x on the side price is moving
    away from for a skewed structure. Wing width defaults to
    config.WING_WIDTH_EM_FRACTION x the day's expected move (floored at
    config.MIN_WING_WIDTH) rather than a fixed number -- a fixed 10-point
    wing (an earlier version of this function) turned the Iron Fly branch
    into an effectively unhedged ATM straddle on low-VIX days; see
    config.py's comment on WING_WIDTH_EM_FRACTION for the backtest evidence.
    """
    if wing_width is None:
        wing_width = max(config.MIN_WING_WIDTH, expected_move * config.WING_WIDTH_EM_FRACTION)
    safe_dist = expected_move * 1.0
    tight_dist = expected_move * 0.6

    strikes: dict = {}
    s = rec.structure
    if s == Structure.IRON_FLY:
        strikes = {"call_short": spot, "call_long": spot + wing_width,
                   "put_short": spot, "put_long": spot - wing_width}
    elif s == Structure.BALANCED_IC:
        strikes = {"call_short": spot + safe_dist, "call_long": spot + safe_dist + wing_width,
                   "put_short": spot - safe_dist, "put_long": spot - safe_dist - wing_width}
    elif s == Structure.SKEWED_IIC:
        if trend_read.trend == Trend.WEAK_BREAKOUT_UP:
            # price drifting up -> tighten the call side (less room), widen put side
            strikes = {"call_short": spot + tight_dist, "call_long": spot + tight_dist + wing_width,
                       "put_short": spot - safe_dist, "put_long": spot - safe_dist - wing_width}
        else:
            strikes = {"call_short": spot + safe_dist, "call_long": spot + safe_dist + wing_width,
                       "put_short": spot - tight_dist, "put_long": spot - tight_dist - wing_width}
    elif s == Structure.PUT_CREDIT_SPREAD:
        strikes = {"put_short": spot - safe_dist, "put_long": spot - safe_dist - wing_width}
    elif s == Structure.CALL_CREDIT_SPREAD:
        strikes = {"call_short": spot + safe_dist, "call_long": spot + safe_dist + wing_width}
    else:
        strikes = {}

    # BUG (found after a live Tradier sandbox order 500'd): every strike
    # above is a raw float computed from spot +/- a fraction of expected
    # move -- e.g. 7749.508330104655. Real SPX/SPXW options only list at
    # config.SPX_STRIKE_INCREMENT-point intervals; an OCC symbol built
    # from an un-rounded strike (tradier_orders.occ_symbol) names a
    # contract that doesn't exist on the exchange, and Tradier's order
    # endpoint has no valid way to fill that -- a very plausible cause of
    # a bare 500 with no other explanation. Round every strike to the
    # nearest real increment before it's used for anything live (backtest
    # P&L uses these same rounded strikes now too, for consistency --
    # the shift versus the previously reported real-data backtest numbers
    # should be small, since it's at most +/-2.5pts of strike placement
    # per leg, but re-run run_backtest.py if you want the exact re-validated
    # figure rather than assuming it's unchanged).
    strikes = {k: round(v / config.SPX_STRIKE_INCREMENT) * config.SPX_STRIKE_INCREMENT
               for k, v in strikes.items()}

    rec.strikes = strikes
    return strikes
