"""
Central configuration for the 0DTE SPX recommendation system.
Tune these from real fill/trade data as you accumulate it -- do not
treat the defaults as validated.
"""

# ---- IV regime breakpoints (VIX level) ----
VIX_LOW_HIGH_BREAK = 20.0     # below: Vajra's old Iron Fly regime
VIX_HIGH_EXTREME_BREAK = 28.0  # above: cut size / consider standing aside

# ---- Opening range ----
OPENING_RANGE_MINUTES = 30    # 15/30/60 all tested in public ORB research;
                               # 30 is a compromise -- revisit with real data
BREAKOUT_CONFIRM_FRACTION = 0.35  # breakout must exceed this fraction of the
                                    # day's expected move to count as "strong"

# ---- Multiple daily entry windows ----
# Per your choice: 2-3 trades/day, evaluated at fixed times regardless of
# whether an earlier window's positions are still open (stacking allowed --
# no automatic cap on concurrent signals). Each window runs the full
# decision pipeline independently, anchored to its own short look-back
# range rather than the day's actual open (only the "morning" window is a
# true opening-range read).
# BE DELIBERATE about the risk math here: with 3 windows x 3 lots each,
# worst case is 9 concurrent lots open if none have closed yet -- roughly
# 3x the single-signal risk this system was originally sized and
# backtested for. Set MAX_CONCURRENT_SIGNALS below if you want a cap.
ENTRY_WINDOWS = [
    {"name": "morning", "range_start_et": "09:30", "range_minutes": OPENING_RANGE_MINUTES},
    {"name": "midday", "range_start_et": "11:30", "range_minutes": 15},
    {"name": "afternoon", "range_start_et": "13:30", "range_minutes": 15},
]
MAX_CONCURRENT_SIGNALS = None  # None = unlimited (your choice); set e.g. 2 to cap stacking

# ---- Econ event timing (used by econ_calendar.py) ----
# How close to a scheduled release -- before OR after -- entries are
# delayed/blocked. Pre-event matters now that entries can happen at any
# time of day, e.g. the "afternoon" window sits right before a 2pm FOMC
# statement.
PRE_EVENT_BLACKOUT_MINUTES = 30

# ---- Expected move ----
TRADING_DAYS_PER_YEAR = 252

# ---- Fill / slippage model ----
# From Vajra: BS theoretical credit ~$4.40, real MM fill ~$1.34 => ~30%.
# Default here is intentionally less punishing than Vajra's observed
# reality so you have room to calibrate down as real data comes in;
# treat 0.5 as a starting placeholder, not a validated number.
FILL_HAIRCUT = 0.5

# ---- Structure selection ----
# Backtest evidence (10yr real SPX/VIX, 2016-09 to 2026-09): the range-bound,
# low-VIX Iron Fly branch (ATM short strikes) was the single worst performer
# by a wide margin -- 54.9% win rate, avg -$211/trade, -$160k total -- while
# every other structure (balanced IC, both credit-spread directions, skewed
# IIC) was solidly positive. This is exactly the branch Vajra used below VIX
# 20. Default is now to use a Balanced IC there too rather than an ATM Iron
# Fly; flip to True only if you want to reproduce the old (worse) behavior.
USE_IRON_FLY_IN_LOW_IV_RANGE_BOUND = False

# ---- Wing width (long-leg protection distance) ----
# BUG HISTORY: earlier version hardcoded a fixed 10-point wing regardless of
# the day's expected move. That's fine at VIX ~30 (CBOE Schwartz's example
# used a ~30-40pt short-strike offset with 10pt wings there) but is far too
# tight at VIX ~15, where it turned the "Iron Fly" branch into effectively an
# unhedged ATM straddle with a razor-thin profit zone -- backtest showed a
# 28% win rate and -258 avg P&L on that branch specifically. Wings now scale
# with the day's expected move instead of a fixed number.
WING_WIDTH_EM_FRACTION = 1.0   # wing width = this fraction of the expected move
MIN_WING_WIDTH = 5.0           # floor so ultra-low-vol days don't get a ~0 wing

# ---- Position sizing ----
BASE_CONTRACTS = 1
SIZE_HALF = 0.5
SIZE_SKIP = 0.0

# ---- Scale-out lot management (live trading) ----
# Each daily signal is split into this many equal-sized lots (contracts),
# same structure/strikes, managed independently after entry.
NUM_LOTS = 3
# One entry per lot, in order: profit target as a fraction of max credit
# captured (None = no fixed target -- the "runner").
LOT_PROFIT_TARGETS = [0.50, 0.70, None]
# Stop-loss for a lot still at its initial risk: close if the debit to close
# reaches this multiple of the credit received (matches your stated CSP
# stop-loss rule, applied here to 0DTE credit structures).
STOP_LOSS_CREDIT_MULTIPLE = 2.0
# Once this many lots (by index, 0-based) have closed at their profit
# target, ratchet the runner's stop to lock in progress instead of still
# risking the full STOP_LOSS_CREDIT_MULTIPLE:
#   after lot 0 (50%) closes -> runner stop moves to breakeven (its own credit)
#   after lot 1 (70%) closes -> runner stop moves to lock RUNNER_LOCK_FRACTION
RUNNER_BREAKEVEN_AFTER_LOTS_CLOSED = 1
RUNNER_LOCK_AFTER_LOTS_CLOSED = 2
RUNNER_LOCK_FRACTION = 0.25  # lock at least this fraction of the runner's own credit
# Defensive exit for the runner independent of the P&L stop: if the
# underlying actually trades through the nearest short strike being
# defended, close the runner right there rather than waiting for the P&L
# stop to catch up -- 0DTE gamma can outrun a polling-interval stop check.
RUNNER_EXIT_ON_STRIKE_TEST = True
# Hard time-based close for anything still open, regardless of P&L --
# 0DTE positions should never be left to expire unmanaged. Eastern Time.
HARD_EOD_EXIT_ET = "15:45"
# How often the live monitor polls open positions for exit conditions.
MONITOR_POLL_SECONDS = 60

# ---- Econ calendar filter ----
EVENT_DELAY_MINUTES_AFTER_RELEASE = 45   # don't sell premium into a fresh
                                           # macro print; wait for it to settle
OPEX_SIZE_MULTIPLIER = 0.5

# ---- Geopolitical / news filter ----
NEWS_RISK_SKIP_THRESHOLD = 3   # risk score 0-3; >=3 => no trade
NEWS_RISK_HALF_SIZE_THRESHOLD = 2

GEOPOLITICAL_KEYWORDS = [
    "invasion", "invades", "war", "missile", "strike on", "airstrike",
    "sanctions", "default", "debt ceiling", "ceasefire collapse",
    "coup", "election unrest", "embargo", "tariff escalation",
    "central bank emergency", "bank failure", "contagion", "downgrade",
    "martial law", "nuclear", "attack",
]

# ---- Data sources ----
TRADIER_BASE_URL_SANDBOX = "https://sandbox.tradier.com/v1"
TRADIER_BASE_URL_PROD = "https://api.tradier.com/v1"
# Free RSS-style headline feeds -- no API key required. Swap/extend freely;
# kept isolated here rather than scattered through news_geopolitical.py.
NEWS_RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "https://finance.yahoo.com/news/rssindex",
]

# ---- Risk-free rate for BS pricing (approx short-term T-bill) ----
RISK_FREE_RATE = 0.045
