"""
Simulates an intraday price path CONSTRAINED to match a day's real
open/high/low/close, because none of the real data sources reachable in
this delivery gave intraday history over 10 years (Yahoo Finance blocks
this sandbox entirely; the Massive/Polygon connector on your Mac gives
only ~2 years of daily bars and no index entitlement -- see the earlier
conversation).

THIS IS A MODELING ASSUMPTION, NOT REAL INTRADAY DATA. It exists so the
scale-out exit logic (profit targets / ratcheting stop / strike-test /
hard EOD close) can be exercised against *something* resembling an
intraday path rather than only end-of-day settlement. Read this module's
limits before trusting results built on it:

- The path is a 4-anchor bridge (open -> {low,high in an assumed order} ->
  close) with Brownian-bridge noise between anchors, clipped so it never
  exceeds the day's real high/low. The order of low/high is a heuristic
  (whichever extreme the close ended up closer to is assumed to have come
  LAST) -- real intraday order can differ arbitrarily and this has no way
  to know it.
- It is deterministic per (date, step_minutes) via a seed derived from the
  date, so re-running produces the same path -- useful for reproducibility,
  not because it's "the" path that actually happened.
- Checked at discrete steps (default every 5 simulated minutes), not
  continuously -- consistent with the ~5min monitoring cadence discussed
  for the live system, but still an approximation of what a real
  continuous price feed would trigger on.
"""
from __future__ import annotations
import hashlib
import math
import random

SESSION_MINUTES = 390  # 9:30 to 16:00 ET


def _seed_for_date(d) -> int:
    return int(hashlib.sha256(str(d).encode()).hexdigest()[:8], 16)


def simulate_intraday_path(date_obj, open_p: float, high_p: float, low_p: float, close_p: float,
                            step_minutes: int = 5, vol_annual: float = 0.15) -> list[tuple[int, float]]:
    """Returns [(minute_offset_from_930, price), ...] from 0 to 390 inclusive."""
    rng = random.Random(_seed_for_date(date_obj))
    n_steps = SESSION_MINUTES // step_minutes
    minutes = [i * step_minutes for i in range(n_steps + 1)]

    if abs(close_p - high_p) <= abs(close_p - low_p):
        anchors_price = [open_p, low_p, high_p, close_p]
    else:
        anchors_price = [open_p, high_p, low_p, close_p]
    # BUG this caught, worth explaining because it's subtle: a FIXED
    # anchor schedule of [0, 0.35, 0.75, 1.0] put the first extreme almost
    # exactly at the "midday" entry window's decision point (135/390 =
    # 0.346, vs the anchor at 0.35) -- meaning every midday entry was
    # systematically opened right at a modeled reversal, every single day,
    # for 10 years. A first full run showed morning +$848k and midday
    # -$843k, an almost perfect mirror image -- far too clean to be a real
    # time-of-day market effect, and it wasn't: it was this coincidence.
    # Fixed by jittering each day's anchor timing (still deterministic per
    # date via the seeded RNG, so reproducible -- just no longer aligned
    # with any fixed decision-time grid).
    anchor1_t = rng.uniform(0.10, 0.50)
    anchor2_t = rng.uniform(anchor1_t + 0.15, 0.90)
    anchors_t = [0.0, anchor1_t, anchor2_t, 1.0]

    sigma_local = open_p * vol_annual / math.sqrt(252) * 0.5

    def bridge(t0, p0, t1, p1, t):
        if t1 <= t0:
            return p1
        frac = (t - t0) / (t1 - t0)
        linear = p0 + (p1 - p0) * frac
        noise = rng.gauss(0, sigma_local) * math.sqrt(max(frac * (1 - frac), 0.0)) * 2
        return linear + noise

    path = []
    for m in minutes:
        t = m / SESSION_MINUTES
        if t <= anchors_t[1]:
            p = bridge(anchors_t[0], anchors_price[0], anchors_t[1], anchors_price[1], t)
        elif t <= anchors_t[2]:
            p = bridge(anchors_t[1], anchors_price[1], anchors_t[2], anchors_price[2], t)
        else:
            p = bridge(anchors_t[2], anchors_price[2], anchors_t[3], anchors_price[3], t)
        p = min(max(p, low_p), high_p)
        path.append((m, p))

    path[0] = (0, open_p)
    path[-1] = (SESSION_MINUTES, close_p)
    return path


def price_at_minute(path: list[tuple[int, float]], minute: int) -> float:
    """Nearest discrete step at or before `minute` (path is a fixed grid)."""
    best = path[0][1]
    for m, p in path:
        if m > minute:
            break
        best = p
    return best


def local_high_low(path: list[tuple[int, float]], start_minute: int, end_minute: int) -> tuple[float, float]:
    window = [p for m, p in path if start_minute <= m <= end_minute]
    if not window:
        window = [price_at_minute(path, start_minute)]
    return max(window), min(window)
