# 0DTE SPX Trade Recommendation System — Design Doc & Code

## 0. Read this first — what this system can and cannot promise

Before any of the mechanics below: a "high win rate, 10-year backtested" 0DTE
options strategy is a red flag phrase in this space, not a feature. Every
published 0DTE iron condor system shows a 65–90% win rate — that is
structural, not a strategy edge. At 0DTE, gamma is enormous near the short
strikes, so the small credits collected on wins are dwarfed by the occasional
large loss. The math (documented in `docs/research_notes.md`) is:

```
7 wins x $300  -  3 losses x $700  =  $0     (breakeven despite a 70% win rate)
```

This is exactly what you already found with Vajra: your Black-Scholes
theoretical credit was $4.40 but real market-maker fills averaged $1.34 —
you were winning most days and still not making money, because realized
credit was ~30% of model credit while realized losses were not similarly
discounted. **Any system that only reports win rate without expectancy,
realistic fill slippage, and tail-loss size is not evaluating the strategy —
it's marketing it.** This system is built to report all four, and the
backtest engine defaults to a conservative fill-slippage haircut you can
calibrate from your own Vajra fill logs.

I could not independently verify "high probability + high win rate,
backtested over 10 years" as a property of any specific rule set — that
depends on strikes, DTE, IV regime, and fill quality, and has to be
demonstrated by running the backtest engine below on real data, not asserted.
What I *can* do, and have done, is: (1) research the best-documented public
approaches, (2) build a decision engine that combines them with your event
and news filters, (3) build a full backtest pipeline, and (4) run it now on
clearly-labeled **simulated** data so you can see it work end-to-end — see
§6 for why real data didn't come from this sandbox and what to feed it
instead.

## 1. What changed vs. Vajra

Vajra ran one structure (Iron Fly below VIX 20 / Breakeven IC at/above VIX 20)
with no view on trend, no event calendar filter, and (per your notes) no
correction for the real-vs-theoretical fill gap. This system adds, as
separate composable filters that all gate the trade:

1. **Trend/regime read at the open** — is today range-bound or trending, and
   how strongly (§2).
2. **Event calendar filter** — FOMC/NFP/CPI/OPEX days get downgraded or
   skipped, never traded blind (§3).
3. **Geopolitical/news risk filter** — headline-keyword + realized-gap based
   risk flag; elevated risk narrows size or skips (§4).
4. **IV regime** — VIX level/percentile picks structure width and credit
   target (§5, folded from Vajra's dual-mode logic).
5. **Strategy selector** — a decision matrix over the above four choosing
   between a balanced Iron Condor, a skewed/unbalanced Iron Condor ("IIC" —
   the informal term for an Inverted/Unbalanced Iron Condor with directional
   skew), a single-side credit spread, an Iron Fly, or **no trade** (§5).
6. **Realistic fill model** in the backtester, calibrated to your own
   Vajra data instead of Black-Scholes mid (§6).

## 2. Trend / opening-range read (at the open)

Based on the Opening Range Breakout (ORB) literature (Option Alpha's
published 0DTE ORB backtest: 60-minute range, 89.4% win rate, profit factor
1.44, max drawdown -$3,453 over their sample — cited so you can judge it
critically, not as proof it'll repeat):

- Measure SPX's high/low over the **first 30 minutes** after the open
  (configurable; code defaults to 30 min as a compromise between the 60-min
  and 15-min variants tested in that piece — 60-min was strongest but delays
  entry).
- Classify:
  - **Range-bound**: price stays inside the opening range, or the range is
    narrow relative to the VIX-implied expected daily move → favors a
    **balanced Iron Condor / Iron Fly**.
  - **Breakout up**: price closes above the opening-range high with
    conviction (range > X% of expected move, volume/trend confirmation) →
    favors a **put-side-only credit spread or a call-skewed (bearish-tilt)
    IIC** — i.e., structure that still profits if price keeps drifting up but
    caps upside risk, per the "skewed iron condor" pattern (widen the side
    you're NOT worried about, tighten/skip the side price is moving toward).
  - **Breakout down**: mirror image, call-side-only spread or put-skewed IIC.
- Expected move for the day is derived from ATM straddle price (or
  VIX/√252 as a fallback), which also sets strike distances.

## 3. Econ calendar filter

Curated list of recurring high-impact US macro events (FOMC decision days,
NFP/jobs report — first Friday, CPI, PCE, ISM), refreshable from a free
public calendar. Rule of thumb encoded in `modules/econ_calendar.py`:

- **FOMC decision day, CPI, NFP before 8:35am ET**: default = **no new 0DTE
  premium-selling trade** until the initial post-release move has printed
  and stabilized (configurable delay, default 45 min) — this mirrors
  Schwartz's CBOE-published guidance to trade 0DTE IC *after* volatility has
  already occurred, not into it.
- **Quad-witching / monthly OPEX**: flagged, size reduced by default, not
  auto-skipped (behaves differently from ad hoc 0DTE days).
- Everything else: normal sizing.

This is a real gap you didn't have in Vajra — Vajra had no concept of "don't
sell premium 20 minutes before CPI prints."

## 4. Geopolitical / news risk filter

Being blunt about the limitation: there is no reliable, free, real-time NLP
geopolitical-risk feed. `modules/news_geopolitical.py` is a **heuristic**,
not a model:

- Pulls headlines from free financial RSS feeds (Reuters/MarketWatch/Yahoo
  Finance top-news style feeds — no API key required, code has the feed URLs
  isolated in one place so you can swap providers).
- Flags a fixed keyword list (war, invasion, strike, missile, sanctions,
  default, ceasefire collapse, coup, election unrest, embargo, tariff
  escalation, etc.) with simple recency weighting.
- Cross-checks against a *market-observable* proxy: overnight SPX futures
  gap and VIX overnight change — if headlines are flagged AND the futures
  gap/VIX move confirms elevated risk, that's a stronger signal than either
  alone; if headlines are flagged but the market shrugged it off, risk score
  is downweighted.
- Output is a 0–3 risk score feeding the selector (0 = normal, 3 = skip the
  day). This is deliberately conservative and crude; treat it as a second
  opinion, not a fact machine, and review flagged headlines yourself before
  the first few weeks of live use.

## 5. IV regime + strategy selector

Reuses your Vajra threshold (VIX 20) as the base IV-regime split, adds a
second breakpoint for elevated vol (VIX 28, roughly the 90th percentile
historically) where credit-selling 0DTE structures are downsized further
regardless of trend read, because tail risk (the actual killer in these
strategies) scales with realized vol much faster than IV-implied credit
does. Full matrix in `modules/strategy_selector.py`; summary:

| Trend | VIX < 20 | 20 ≤ VIX < 28 | VIX ≥ 28 |
|---|---|---|---|
| Range-bound | Iron Fly (ATM) | Balanced IC | Balanced IC, half size |
| Weak breakout | Balanced IC, wider tested side | Skewed IIC | Skewed IIC, half size |
| Strong breakout | Single-side credit spread | Single-side credit spread | No trade |
| Event/news flag ≥ 2 | Delay / half size | Delay / half size | No trade |

## 5b. Real 10-year backtest result (2026-09-08, real SPX/VIX from Yahoo Finance)

The user supplied real `^GSPC`/`^VIX` daily history, 2016-09-06 through
2026-09-04 (2515 trading days). Three runs, in order, each fixing something
the previous one exposed:

| Run | Change | Win rate | Expectancy/trade | Total P&L (10yr) | Profit factor | Max drawdown |
|---|---|---|---|---|---|---|
| 1 | Original wing width (fixed 10pt) | 70.5% | -$59.63 | -$144,360 | 0.58 | -$144,700 |
| 2 | Wing width scaled to expected move | 79.8% | +$22.01 | +$53,296 | 1.08 | -$55,620 |
| 3 | + Iron Fly branch disabled (see below) | 92.7% | +$166.16 | +$402,265 | 2.58 | -$16,111 |

**What run 1 exposed**: a fixed 10-point wing width was fine at CBOE's
example VIX (~30) but far too tight at VIX ~15, turning the Iron Fly branch
into an unhedged ATM straddle. Fixed by scaling wing width to the day's
expected move (`config.WING_WIDTH_EM_FRACTION`).

**What run 2 exposed, after that fix**: even with sensible wings, the
Iron Fly (ATM short strikes, range-bound + VIX<20) was still the one clearly
losing structure — 54.9% win rate, avg -$211/trade, -$160k total over 10
years — while every other structure (Balanced IC, both credit-spread
directions, skewed IIC) was solidly positive individually. **This is the
exact branch Vajra used below VIX 20.** `config.USE_IRON_FLY_IN_LOW_IV_RANGE_BOUND`
now defaults to `False`, replacing it with a Balanced IC in that regime.

Run 3's numbers (92.7% win rate, positive expectancy every year except 2018,
no single day or handful of days driving the total, profit factor comfortably
above the ~1 breakeven line implied by the avg-win/avg-loss ratio) are the
most credible result produced so far. Still read them with real skepticism:

- Options are still priced synthetically (Black-Scholes off daily OHLC +
  VIX, with the 0.5 fill haircut) — **not replayed from real historical
  0DTE quotes.** The fill haircut is an assumption, not a measurement.
- The trend classifier only sees the overnight gap (to avoid lookahead
  bias, see §6/§7) — a materially weaker signal than a true intraday
  opening-range read, so ~65% of trades ended up Balanced IC by default
  (trend rarely triggered a breakout classification on gap alone). A live
  version with real 1-minute opening-range bars would likely use the
  breakout/skewed structures far more often, for better or worse.
- One bad multi-week stretch can still cost more than several good months
  made (see the "worst days" list in the generated report) — size for that,
  don't extrapolate the smooth-looking equity curve.
- 10 years, even real, is one historical path, not a distribution — it
  includes 2018's vol spike and 2020's COVID crash but not every regime
  that could occur.

Next validation step, if you want it: get real historical SPX 0DTE options
quotes (Massive/Polygon options entitlement, CBOE DataShop, or your own
Vajra trade logs) and replace the Black-Scholes-plus-haircut pricer with
actual historical fills for the same date range — that's the test that
would tell you whether the 0.5 haircut assumption is even in the right
neighborhood.

## 6. Backtest engine — what's real vs. simulated, and why

**This sandbox's outbound network is allowlisted to package registries only
(PyPI, npm, etc.) — Yahoo Finance and Stooq were both refused by the proxy
(403) when this was built (2026-09-08), so I could not pull real SPX/VIX
history from here.** Two ways forward, and I'd only trust results from the
first:

1. **You supply real data** — export 10 years of SPX (or SPY as a x10 proxy)
   and VIX daily OHLC, plus, if you have it, historical 0DTE options fills
   (Tradier historical, CBOE DataShop, ORATS, or your own Vajra trade logs)
   as CSVs, and `run_backtest.py --spx path.csv --vix path.csv` runs the real
   thing.
2. **Demonstration run included in this delivery** uses a clearly-labeled
   **synthetic** SPX/VIX path (GBM + a simple vol-clustering overlay,
   calibrated to realistic long-run SPX drift/vol, NOT fit to real history)
   purely to prove the pipeline runs end-to-end and produces a coherent
   report. Its win rate/expectancy numbers describe the simulation, not the
   market, and are labeled as such in the report header — do not use them to
   decide whether to trade this live.

The fill model (`modules/option_pricer.py`) prices 0DTE strikes with
Black-Scholes and then applies a **haircut factor** (default 0.5, i.e.
assume you realize half of theoretical credit on entry and pay up on exits)
— you told me Vajra's real fills averaged ~30% of BS theoretical, so once you
have a few weeks of this system's paper/live fills, recalibrate
`FILL_HAIRCUT` in `config.py` to match reality rather than trusting the
default.

## 7. Files

```
zerodte_system/
  config.py                      # thresholds, VIX breakpoints, haircut, keyword list
  modules/
    market_open.py                # opening-range trend classifier
    market_structure.py            # gap/VWAP/EMA/ATR/realized-vol/range-acceptance
    vol_structure.py                # VIX/IV-vs-RV/skew/gamma-exposure ("gamma wall")/expected-move distance
    scoring.py                      # composite trend score, bullish/range probability, regime labels
    econ_calendar.py              # FOMC/NFP/CPI/OPEX calendar + filter
    news_geopolitical.py          # RSS headline fetch + keyword/market-confirm risk score
    iv_regime.py                  # VIX level/percentile classifier
    option_pricer.py              # Black-Scholes 0DTE pricer + fill haircut
    strategy_selector.py          # decision matrix -> recommended structure + strikes
    backtest_engine.py            # historical or synthetic backtest runner
    data_sources.py                # Tradier client + synthetic data generator
  run_backtest.py                 # CLI: python run_backtest.py [--spx csv --vix csv]
  run_live.py                     # CLI: morning entry (Tradier sandbox + Telegram), run once near the open
  run_monitor.py                  # CLI: position monitor (Tradier sandbox + Telegram), run every ~1-5 min
  state/                           # per-day JSON position state, written by run_live.py, read/updated by run_monitor.py
  requirements.txt
  docs/research_notes.md          # sources consulted, with links
reports/
  backtest_report_<timestamp>.md  # generated by run_backtest.py
```

## 7b. Live trading: guardrails, cadence, Telegram + Tradier sandbox

Decisions made with you and implemented:

- **Execution mode**: Tradier **sandbox only** (`run_live.py`/`run_monitor.py`
  hardcode `sandbox=True` — this does not touch a production Tradier
  account).
- **Trades per day**: one signal per day (one opening-range read, one
  structure/strikes decision), matching the design throughout this doc.
- **Contracts per signal**: split into `config.NUM_LOTS` (default 3) equal
  lots of the *same* structure/strikes, managed independently after entry:
  - Lot 0: closes at 50% of max credit captured.
  - Lot 1: closes at 70% of max credit captured.
  - Lot 2 (the "runner"): no fixed profit target. Its stop-loss ratchets
    as the other lots bank profit — starts at the standard 2x-credit stop,
    moves to breakeven once lot 0 closes, then locks in 25% of its own
    credit once lot 1 also closes (`RUNNER_LOCK_FRACTION`). It also has an
    independent defensive exit if the underlying actually trades through
    the short strike it's defending (`RUNNER_EXIT_ON_STRIKE_TEST`) —
    added on top of what was asked for, because 0DTE gamma can blow
    through a polling-interval P&L stop faster than the poll notices.
  - All lots get a hard time-based close (`HARD_EOD_EXIT_ET`, default
    15:45 ET) regardless of P&L — nothing is left to expire unmanaged.
  - Full logic + a passing smoke test: `modules/position_manager.py`.
- **Stop-loss for non-runner lots**: 2x credit received, per your stated
  CSP rule adapted to 0DTE (`config.STOP_LOSS_CREDIT_MULTIPLE`).
- **Telegram**: `modules/telegram_alerts.py`, same plain-HTTP-bot-API
  pattern as Vajra. Set `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`; alerts
  just print to the console if unset rather than failing the run.
- **Tradier order placement**: `modules/tradier_orders.py` builds multileg
  entry/exit payloads (OCC symbol construction, credit-spread leg sides)
  and submits them to the sandbox account. **Not yet run against a live
  Tradier sandbox account** — this delivery environment has no network
  path to Tradier — so treat the order-submission code as a strong draft,
  not a verified integration. Test it yourself starting with 1 contract
  before trusting the loop unattended, and double-check the OCC symbol
  builder's output against Tradier's own `/markets/options/lookup` for a
  couple of real SPXW contracts first — that's the single most likely
  place for a silent mismatch.
- **Two separate scripts, not one long-running process**: `run_live.py`
  (once, near the open: decide + enter) and `run_monitor.py` (repeatedly,
  e.g. every `config.MONITOR_POLL_SECONDS` via a scheduled job) —
  matches how Vajra runs on GitHub Actions, since Actions doesn't support
  a persistent always-on worker well. State is handed between them via a
  small JSON file per day in `state/`.

## 7c. Multiple daily entries (2-3/day), stacking, and event timing fix

Per your follow-up: instead of one entry per day, `config.ENTRY_WINDOWS`
now defines three independent decision points (`morning` 09:30, `midday`
11:30, `afternoon` 13:30 ET by default, each with its own short look-back
range), and — per your choice — they **stack**: an afternoon entry doesn't
wait for the morning one's lots to close first. `run_live.py` now takes
`--window {morning|midday|afternoon}` and is meant to run three times a day
(three separate scheduled steps); `run_monitor.py` now watches *all* of
today's open signals at once (one state file per window,
`state/<date>_<window>.json`), not just one.

**Be deliberate about what stacking means for risk**: with 3 windows x 3
lots each, the worst case is 9 concurrent lots open at once if none have
closed yet — roughly 3x the per-signal risk this system was originally
sized and backtested for (the backtest above still models exactly 1
signal/day; it has not been re-run for the 3-window/stacking case). If
you want a ceiling, set `config.MAX_CONCURRENT_SIGNALS` (e.g. `2`) and
`run_live.py` will skip a new window's entry rather than stack past it —
left at `None` (unlimited) by default because that's what you chose, but
flagging that this is the one parameter most worth revisiting once you've
watched it run for a few days.

**A real bug this surfaced and fixed**: the event-calendar filter used to
assume every high-impact release happens near the market open (a fixed
"60 minutes after open" delay check). That was harmless with only one
morning entry, but wrong the moment an `afternoon` window (13:30 ET) can
sit right before a 2pm FOMC statement — the old logic would never have
delayed that entry. `modules/econ_calendar.py` now computes minutes
before/after the *actual* release clock time (FOMC 14:00 ET, CPI/NFP 08:30
ET) and blocks entries within `config.PRE_EVENT_BLACKOUT_MINUTES` (30) before
or `config.EVENT_DELAY_MINUTES_AFTER_RELEASE` (45) after — verified with
unit checks: a 13:45 ET FOMC-day entry is now correctly delayed, a 09:30 ET
FOMC-day entry is not (release is hours away), and CPI-day delay/no-delay
both switch correctly at the 45-minute mark. This has zero effect on the
10-year backtest above (it never populated real FOMC/CPI dates), but
matters as soon as you feed it a real calendar.

## 7d. Monitoring cadence — the honest limitation, discussed and accepted

Per your question about live monitoring: `run_monitor.py` does check
profit targets, the ratcheting stop, the runner's strike-test defensive
exit, and the hard EOD close, and does close positions + alert the moment
any of those trip. But it is polling-based, not a continuous tick stream.
You chose GitHub Actions cron (~5 min realistic polling, same pattern as
Vajra) over a persistent always-on loop, which is the simpler and
reasonable place to start — just keep in mind a fast intraday move can go
against an open lot for up to that ~5 minutes before the next run catches
it. If that gap ever looks too wide once you're watching it live (e.g. it
would have missed catching a move like the backtest's worst days), the fix
is switching the poll source to a persistent worker at
`config.MONITOR_POLL_SECONDS` — the monitor's decision logic itself
doesn't change, only what invokes it and how often.

## 7e. Richer market read: structure, vol, real calendar, composite score

Per your follow-up list, here's what exists now and what's still a gap:

**Already matched your #5 (intraday confirmation) before you asked**: the
morning window's decision already happens at `range_start + OPENING_RANGE_MINUTES`
(10:00 ET by default), not at 9:30:01 — the opening range needs to develop
first. No change needed there, just confirming.

**#1 Overnight/opening structure** — new `modules/market_structure.py`:
gap %, VWAP (typical-price, volume-weighted from the window's own bars),
9/21 EMA alignment, ATR(14), realized volatility (10-day annualized,
close-to-close — distinct from VIX, which is IV-implied), and a
range-acceptance/rejection classification (holding above/below the prior
day's range and today's opening range, vs. just testing it). Breadth is
the one item on your list **not** implemented — it needs an
advance/decline or up/down-volume feed Tradier's index/equity endpoints
don't directly give you; flagging rather than faking it.

**#2 Options/vol structure** — new `modules/vol_structure.py`: VIX level
+ change, expected move (already existed), ATM IV pulled from the day's
0DTE chain, IV-vs-realized-vol ratio, a 25-delta-ish skew proxy, distance
to the expected-move boundaries, and a gamma-exposure-by-strike estimate
with a "gamma wall" pick. **Read the module's docstring before trusting
the gamma wall**: it's the standard retail GEX approximation (OI x gamma x
spot² with an assumed dealer-positioning sign convention) — genuinely
useful as a support/resistance heuristic, but it is not real dealer
positioning data, which nobody outside the market makers has. Real GEX
vendors (SpotGamma etc.) refine this same idea; this is the transparent,
unrefined version of it.

**#3 Macro-event engine** — confirmed against the Fed's and BLS's own
published schedules (checked 2026-09-08): FOMC Sep 15-16 (decision Sep 16,
2pm ET), PPI Sep 10, CPI Sep 11, all correct as you stated. Loaded real
2026 dates for FOMC/CPI/PPI/NFP through year-end into `econ_calendar.py`
(`FOMC_DATES_2026`, `CPI_DATES_2026`, `PPI_DATES_2026`, `NFP_DATES_2026`),
now wired into `run_live.py` by default instead of the empty placeholder
lists. **Gap**: PPI is now a real event type; ISM, JOLTS, GDP, and PCE are
not implemented as distinct types yet (all have fixed public release
schedules like CPI/PPI and could be added the same way — ISM in
particular is a quick add if you want it). Fed speakers are not
implemented at all — there's no clean free API for the speaker calendar
the way there is for scheduled data releases; this would need a manually
maintained watch-list or a paid calendar feed.

**#4 News/geopolitical risk** — the existing keyword + market-confirmation
heuristic (`news_geopolitical.py`) already covers war/invasion, sanctions,
tariffs, central-bank/embargo language; energy-shock and explicit
"government/political developments" phrasing weren't in the original
keyword list and are worth adding if you see them matter in practice —
easy to extend, just a list in `config.GEOPOLITICAL_KEYWORDS`.

**Composite score + your exact Telegram format** — new `modules/scoring.py`
combines all of the above into a -100..+100 trend score, a volatility
regime label, an event-risk label, and "bullish probability"/"range
probability" percentages, formatted via
`telegram_alerts.format_context_alert()` to match the style you sketched
(Trend / Volatility / Event risk / Expected move / Opening structure /
Gamma resistance / Range probability / Bullish probability). **Read
`scoring.py`'s docstring before treating these as real probabilities**:
they're a transparent, hand-weighted combination squashed through a
logistic function for readability — not a model fitted to historical
outcomes, and not validated against how often a "69% bullish" day actually
closed higher. That validation would mean logging these scores live for a
few months and checking them against what actually happened, which hasn't
been done. Offline smoke-tested with synthetic bar/chain data (module
compiles, produces the expected format and internally consistent numbers)
but not run against a live Tradier feed.

## 7f. Intraday multi-window stacking backtest — built, but not yet trustworthy

You asked (and I confirmed "yes") for a backtest that actually models the
live design: 2-3 entries/day at fixed windows, stacking allowed, 3
scale-out lots per signal with the ratcheting stop — as opposed to
`run_backtest.py`'s single-signal, hold-to-close model. That's now
`run_backtest_intraday.py` / `modules/backtest_intraday.py`, reusing the
exact same trend/event/structure/exit logic the live scripts use.

It needed one new piece that didn't exist before: a per-day **simulated**
intraday price path (`modules/intraday_path.py`), since no real
minute-level history was reachable for the full 10-year window (see §6 —
Yahoo blocks this sandbox entirely, and Massive's index data, per the
research below, doesn't go back further than ~2023 regardless of plan).
The path is a 4-anchor Brownian-bridge constrained to match each day's
real open/high/low/close — useful for exercising the exit logic, but it is
a modeling assumption, not observed intraday fact.

**Three real bugs were found and two were fixed** while getting this to a
runnable state:

1. **Unit-consistency stop bug** — the exit debit was first priced by
   dividing by `FILL_HAIRCUT` (stacking a second discount on the
   already-haircut entry credit), then by pricing at plain theoretical mid
   with no haircut at all — the second attempt still failed because
   `STOP_LOSS_CREDIT_MULTIPLE(2.0) x FILL_HAIRCUT(0.5) = 1.0` exactly, so
   the stop threshold equaled the un-haircut entry price with zero
   cushion, tripping every lot instantly. Fixed by pricing the exit debit
   in the same haircut-adjusted units as the entry credit. **Fixed.**
2. **Range-window included the decision minute itself** — so "current
   price" was structurally guaranteed to fall inside the measured
   range, making breakout detection impossible (a first full run showed
   100% Balanced Iron Condor, zero breakout structures, across 22,611
   lots). Fixed by measuring the range up to (but excluding) the decision
   minute. **Fixed.**
3. **Anchor-timing artifact** — the first fixed anchor schedule
   (`[0, 0.35, 0.75, 1.0]`) landed almost exactly on the midday window's
   decision fraction (135/390 = 0.346), so every midday entry opened right
   at a modeled reversal, every day, for 10 years — producing an almost
   perfect mirror image (morning +$848,306 vs. midday -$842,785) that was
   an artifact, not a real time-of-day effect. Jittering the anchor timing
   reduced the total P&L swing across three configurations
   (-$101,724 -> -$13,932 -> +$3,759) but **did not fix the deeper issue**:
   the noise formula's `sqrt(frac x (1-frac))` shape is lowest near the
   session boundaries and highest mid-session — the opposite of real
   intraday markets' typical U-shaped volatility — which structurally
   favors the morning window (always in the model's "calm zone") no
   matter how the anchors are jittered. Morning stayed positive
   (~$635k-$650k) across every configuration tried. **Not fixed** —
   diagnosed and reported, not patched around.

**Bottom line: this backtest cannot yet tell you whether the 2-3-trades/day
stacking design helps or hurts.** The per-window P&L split is dominated by
a synthetic-path artifact, not by anything about your actual trading
hours. `run_backtest.py`'s single-signal result (+$402,265 total, 92.7%
win rate, PF 2.58, max DD -$16,111) remains the one number here I'd
actually trust, because it never needed an intraday path — it decides
once, at the open, and holds to the real daily close.

### What real intraday data would take (researched per your "yes")

Two candidate real sources, compared honestly:

**Massive.com (formerly Polygon.io) — ruled out for this use case.**
Their Indices product (SPX, VIX, etc.) comes in three tiers — Basic
($0/mo, EOD only), Starter ($49/mo, 15-min delayed), Advanced ($99/mo,
real-time, "non-pros only") — and **every tier's historical depth is the
same: data starts in early March 2023**, roughly 3.5 years back from
today, regardless of which plan you pay for. That's a hard floor, not a
paywall — no Massive plan reaches back to 2016. This matches what your
account's own entitlement already showed directly: `I:SPX`/`I:VIX`
endpoints returned 403 NOT_AUTHORIZED on your current plan, and even the
stock-proxy endpoints (SPY/VIXY) that did work were capped at 500 daily
bars (~2 years). Massive/Polygon is a fine source for a live feed once
this trades for real, or for validating recent months — it cannot supply
the 10-year historical window this backtest needs.

**Cboe DataShop — the only real path to a 10-year window, cost not fully
pinned down.** Their Option Quotes product (`datashop.cboe.com/option-
quote-intervals`) is OPRA-fed, covers index options, and offers
1/5/10/15/20/30/60/390/405-minute intraday intervals, explicitly
"Available from January 2012 to present" — that covers the entire
2016-2026 backtest window. Two catches: (1) SPX index underlying bid/ask
specifically requires an active Cboe Global Indices Feed (CGIF) license,
with fees "starting at $1,000/month" per their own materials; (2) the
per-dataset purchase price is cart-based (it shows "$0.00" until you pick
specific symbols/dates) and their FAQ confirms there's no published price
list — getting an exact number means calling their sales line
(+1 800 307-8979) or using the site's Sales contact form. So: real,
available back to 2012, but likely $1,000+/month in licensing alone before
any per-dataset cost, and the exact total isn't obtainable without talking
to a human there.

**My honest recommendation**: don't spend on CBOE DataShop just to
validate the stacking design in a backtest — a four-figure-plus monthly
license is a lot to spend purely to answer "does trading 3 windows instead
of 1 help or hurt," when the answer might just as easily be "trade the
single validated signal and treat multi-window as a live experiment you
watch, not something you backtest first." If you do want to spend on real
intraday data eventually, it's more likely worth it once this is trading
live capital (for post-trade analysis / fill-quality calibration against
your actual Tradier fills) than to unstick this specific backtest.

## 7g. Alert branding + daily/weekly/monthly P&L summaries

Per your request:

**Every Telegram alert now carries a `Claude` prefix** — one line added in
`telegram_alerts.send()` (`ALERT_PREFIX = "*Claude* 🤖"`), so it's the only
place that needed to change; every `format_*` helper (context read, entry,
exit, EOD, period summary) stays prefix-free and just returns its own
body. If you ever want a different label or to drop the emoji, that's the
one constant to edit.

**Daily/weekly/monthly P&L summaries** — new `modules/pnl_log.py` is an
append-only CSV (`state/pnl_log.csv`) written by `run_monitor.py` the
moment every lot in a signal fully closes (one row per signal: date,
window, structure, total P&L, lot count). On the run that closes the
*last* still-open signal for the day, `run_monitor.py` sends a daily
summary automatically, and additionally a weekly summary on Fridays and a
monthly summary on the last trading day of the month (`is_last_weekday_
of_week` / `is_last_trading_day_of_month` in `pnl_log.py`). **Caveat worth
reading before trusting the auto-trigger**: those two triggers assume the
last trading day of the week/month is the calendar Friday / last Mon-Fri —
there's no market-holiday calendar behind them, so a holiday landing
exactly on that day means that period's summary fires a day off (or not at
all) that one time; the aggregation itself (which rows fall in which
week/month) is unaffected either way, since it uses the actual dates
present in the log. If that ever matters, call `pnl_log.weekly_summary()`/
`monthly_summary()` directly against the log for the exact range you want.

## 7h. Precision upgrades (2026-09-09): limit orders, live econ calendar, Claude news scoring

Three things added after the first live trade (which used a market order
and the 2026-only hardcoded calendar):

**Limit orders instead of market orders.** That first live entry used
`order_type="market"` -- `tradier_orders.py` itself flagged this as risky
for a 4-leg spread's fill quality, and it was the right thing to fix
before trusting more real fills. `run_live.py` now submits entries as a
`"credit"`-type limit order at `config.ENTRY_LIMIT_PRICE_FRACTION` (0.90
default) of the full theoretical credit, then polls briefly
(`config.ORDER_FILL_POLL_ATTEMPTS` x `ORDER_FILL_POLL_SECONDS`) for a
confirmed fill and, if one lands in time, overwrites that lot's recorded
credit with the REAL fill price rather than the pre-trade estimate --
everything downstream (profit targets, the ratcheting stop) is keyed off
that number, so this makes the whole exit chain more accurate whenever a
fill is confirmed. If it isn't confirmed in time, the order is left
alone (`duration="day"`, so it can still fill later) and the pre-trade
estimate is kept as a documented fallback -- never a crash.
`run_monitor.py`'s exits are split by urgency: a profit-target close is
discretionary (a limit order, willing to pay up to
`config.EXIT_LIMIT_PRICE_FRACTION` more than the last mid to still get
filled promptly) but a stop-loss/strike-test/hard-EOD close stays a
market order always -- those need certainty of getting out now more than
they need a good price, and a limit order that fails to fill while the
position keeps moving is strictly worse than market-order slippage.
UNTESTED against a real fill confirmation (the `avg_fill_price` field
name in `get_order_status()` is a documented best guess) -- watch the
first few real limit-order trades closely.

**Live econ calendar instead of hardcoded 2026 dates.** New
`modules/econ_calendar_fetch.py` scrapes the Fed's own FOMC calendar page
and BLS's own release-schedule pages (both publish full-year schedules
far in advance, no paid calendar needed) and `refresh_econ_calendar.py`
writes the result to `data/econ_calendar_cache.json`, refreshed weekly by
`.github/workflows/calendar_refresh.yml` (decoupled from every trading
run on purpose -- a scraper break here can never delay or corrupt an
actual entry decision). `econ_calendar.py`'s new `load_calendar_dates()`
prefers that cache, merged with the original hardcoded 2026 lists, and
falls back to hardcoded-2026-only if the cache is missing/corrupt.
**Honesty about this one specifically**: it was written and validated
against page CONTENT (fetched through a separate tool) in an environment
with no general internet access of its own, so the actual `requests`/
`BeautifulSoup` scraping code in `econ_calendar_fetch.py` has never run
against the live pages. It should work from GitHub Actions (which has
normal internet access), but run `calendar_refresh.yml` manually once and
read `data/econ_calendar_cache.json` yourself before trusting it
unattended -- see that module's docstring for the exact caveats and the
fail-safe design (a bad scrape leaves the old cache/hardcoded list alone
rather than writing wrong data).

**Anthropic API key for news/geopolitical scoring.** `news_geopolitical.
py`'s original keyword scan is still there and still the default -- if
you add an `ANTHROPIC_API_KEY` repo secret, `score_news_risk()`
(what `run_live.py` now calls) tries Claude first: it reads the actual
fetched headlines and judges SPX-relevant risk instead of matching a
fixed word list, which catches things no keyword list anticipated. Any
failure (no key, network, bad response) falls straight back to the
keyword scan -- this is a strict upgrade-if-available, never a new
failure mode for the entry decision. Optional; the system runs exactly
as before if you don't add the secret.

Package additions for all three: `beautifulsoup4`, `lxml`, `anthropic`
(now in `requirements.txt`).

## 8. Honest next steps

1. Give me real SPX/VIX (and ideally 0DTE options) history, or point me at
   an account you have with Tradier/ORATS/CBOE DataShop, and I'll re-run
   the backtest on real data and report real numbers — including the ones
   that make the strategy look bad, if that's what the data shows.
2. Decide the fill haircut from your actual Vajra trade log rather than
   my default guess.
3. Before this trades live capital: paper-trade the recommendation for at
   least 4–6 weeks including at least one FOMC and one NFP day, and compare
   the system's own predicted vs. realized fills.

## 9. Deploying on GitHub Actions

Two workflows in `.github/workflows/`:

- **entry.yml** — fires `run_live.py --window <name>` once per entry
  window (morning/midday/afternoon), on the cron schedule matching
  `config.ENTRY_WINDOWS`'s decision times. Also runnable manually
  (`workflow_dispatch`, pick a window) for testing before relying on cron.
- **monitor.yml** — fires `run_monitor.py` roughly every 5 minutes across
  the 09:30-16:00 ET session.

Both commit anything they write under `state/` (signal JSON, `.done`
renames, `pnl_log.csv`) back to the repo at the end of the run, since
GitHub Actions runners are ephemeral and don't share a filesystem between
runs — the repo itself is the persistence layer. They share one
`concurrency` group so a push from one never races a push from the other.

**Cron times are UTC and assume US Eastern Daylight Time (EDT, UTC-4)** —
correct as of this writing (Sept 2026), but they will be off by 1 hour
after the US DST transitions (~first Sunday of November, and again
~second Sunday of March) until you edit the cron lines by ±1 hour. Both
workflow files' comments spell out the exact ET-to-UTC mapping.

**GitHub's scheduled triggers are best-effort**, not guaranteed to the
minute — expect occasional multi-minute slippage, more under platform
load. That's consistent with the ~5-minute monitoring cadence already
discussed and accepted (§7d); it is not millisecond-precise execution,
and a fast intraday move can outrun it before the next scheduled run
catches it.

Setup, step by step:

1. Create a GitHub repo (private recommended, since this touches your
   Tradier/Telegram credentials via secrets — never commit tokens
   directly into any file).
2. Push this codebase to it (see the chat for the exact git commands).
3. Repo Settings -> Secrets and variables -> Actions -> New repository
   secret, add all four: `TRADIER_TOKEN`, `TRADIER_SANDBOX_ACCOUNT_ID`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Optionally also add
   `ANTHROPIC_API_KEY` (see §7h) for Claude-based news scoring instead of
   the keyword fallback -- everything works without it.
4. Repo Settings -> Actions -> General -> Workflow permissions -> "Read
   and write permissions" (needed so the workflows can commit `state/`
   back to the repo; the `permissions: contents: write` block in each
   workflow file needs this org/repo-level setting enabled too).
5. Actions tab -> run `0DTE Entry` manually via "Run workflow" (pick a
   window) and `0DTE Monitor` manually, once each, against your Tradier
   SANDBOX account, before trusting tomorrow's cron -- these scripts have
   not been run end-to-end against a live Tradier feed before now (see
   §7b), so a manual dry run is the cheapest place to catch a wiring
   mistake.
6. Once both manual runs succeed (Telegram alerts arrive, `state/`
   changes get committed), the cron schedules take over automatically —
   nothing else to do for tomorrow's session.

### `--dry-run`: testing `run_live.py` outside market hours

A real entry decision needs today's actual intraday bars for that window
-- if the window hasn't happened yet (e.g. testing at midnight for a
10:00 ET decision), no real market data source can return it; that's not
a bug, it would be true of any system wired to a live feed, and Tradier
correctly rejects the request rather than inventing data. That is a
separate thing from "can I test this system whenever I want" -- for that,
`run_live.py --window <name> --dry-run` (also exposed as a checkbox on
the "0DTE Entry" workflow's manual "Run workflow" button) transparently
falls back to the most recent completed trading day's data when today's
window hasn't closed yet, runs the full pipeline against it end to end
(structure selection, strikes, sizing, both Telegram alerts, clearly
marked `[DRY RUN]`), and then deliberately stops: no Tradier order is
submitted, no `state/` file is written, no `pnl_log.csv` row is added.
It proves the wiring works; it cannot and does not claim to predict what
today's real numbers will be, since those don't exist yet.
