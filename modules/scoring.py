"""
Composite scoring for the Telegram alert: a -100..+100 trend score, a
"bullish probability", and a "range probability" in the style you sketched.

BE HONEST ABOUT WHAT THESE NUMBERS ARE: they are a transparent, hand-built
weighted combination of the market_structure.py / vol_structure.py signals,
squashed into probability-looking numbers with a logistic function. They
are NOT the output of a model fitted to historical outcomes, and they have
NOT been validated against how often "bullish probability: 69%" days
actually closed higher. Report them for what they are -- a readable summary
of the inputs that went into today's structure choice -- not as a
calibrated forecast. If you want a real forecast probability eventually,
that requires logging these scores live for a few months and checking
them against realized outcomes, which this delivery has not done.
"""
from __future__ import annotations
from dataclasses import dataclass
import math

from modules.market_structure import MarketStructure
from modules.vol_structure import VolStructure


@dataclass
class CompositeScore:
    trend_score: int              # -100..+100
    trend_label: str              # "bullish" / "bearish" / "neutral"
    bullish_probability_pct: float
    range_probability_pct: float
    volatility_regime: str        # "low"/"moderate"/"elevated"/"extreme" + contracting/expanding
    event_risk_label: str         # "low"/"medium"/"high"


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_trend_score(ms: MarketStructure, breakout_frac_of_expected_move: float) -> int:
    """Weighted sum of independent directional signals, each contributing
    on a comparable scale, then clipped to [-100, 100]. Weights are
    judgment calls, not fitted -- documented here so they're easy to
    revisit:
      - price vs VWAP: +/-30
      - EMA alignment (9 over 21, price over both): +/-25
      - opening-range breakout direction & strength: +/-30 x frac (capped at 1)
      - gap direction (overnight positioning): +/-15, tapered by ATR-relative size
    """
    score = 0.0
    if ms.price_vs_vwap == "above":
        score += 30
    elif ms.price_vs_vwap == "below":
        score -= 30

    if ms.ema.get("aligned_bullish"):
        score += 25
    elif ms.ema.get("aligned_bearish"):
        score -= 25

    frac = _clip(breakout_frac_of_expected_move, 0.0, 1.0)
    if "above prior range" in ms.range_acceptance or "above opening range" in ms.range_acceptance:
        score += 30 * frac
    elif "below prior range" in ms.range_acceptance or "below opening range" in ms.range_acceptance:
        score -= 30 * frac

    if not math.isnan(ms.atr_14) and ms.atr_14 > 0:
        gap_atr_ratio = _clip(ms.gap_pct / 100.0 * 1.0, -1.0, 1.0)  # gap_pct already a %, keep modest weight
        score += 15 * gap_atr_ratio

    return int(round(_clip(score, -100, 100)))


def trend_label(score: int) -> str:
    if score >= 15:
        return "bullish"
    if score <= -15:
        return "bearish"
    return "neutral"


def bullish_probability(score: int) -> float:
    """Logistic squash of the trend score around 50%. Deliberately gentle
    (k=0.035) so a +100 score doesn't claim near-certainty -- see module
    docstring on why this is a readability transform, not a forecast."""
    k = 0.035
    p = 1.0 / (1.0 + math.exp(-k * score))
    return round(_clip(p * 100.0, 5.0, 95.0), 1)


def range_probability(score: int, base_rate_pct: float = 68.0) -> float:
    """Baseline ~68% is the 1-sigma "stays within the expected move" rate
    under a rough normal approximation; scaled down as the trend score's
    magnitude grows, since a strongly trending open makes staying inside
    the expected-move band less likely, not more."""
    damp = 1.0 - (abs(score) / 100.0) * 0.5  # up to 50% damping at |score|=100
    return round(_clip(base_rate_pct * damp, 10.0, base_rate_pct), 1)


def volatility_regime_label(vix: float, vix_change_pct: float) -> str:
    if vix < 15:
        level = "low"
    elif vix < 20:
        level = "moderate"
    elif vix < 28:
        level = "elevated"
    else:
        level = "extreme"
    direction = "expanding" if vix_change_pct > 2 else ("contracting" if vix_change_pct < -2 else "flat")
    return f"{level} / {direction}"


def event_risk_label(event_is_blocking: bool, news_score: int) -> str:
    if event_is_blocking or news_score >= 3:
        return "high"
    if news_score >= 1:
        return "medium"
    return "low"


def compute_composite_score(ms: MarketStructure, vs: VolStructure, breakout_frac: float,
                             event_is_blocking: bool, news_score: int) -> CompositeScore:
    score = compute_trend_score(ms, breakout_frac)
    return CompositeScore(
        trend_score=score,
        trend_label=trend_label(score),
        bullish_probability_pct=bullish_probability(score),
        range_probability_pct=range_probability(score),
        volatility_regime=volatility_regime_label(vs.vix, vs.vix_change_pct),
        event_risk_label=event_risk_label(event_is_blocking, news_score),
    )
