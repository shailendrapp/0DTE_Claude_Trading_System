# Research notes

Sources consulted while designing this system (2026-09-08). Read critically —
none of these are peer-reviewed, and several are vendor/blog content with an
incentive to make 0DTE strategies look good.

- [Henry Schwartz's Zero-Day SPX Iron Condor Strategy: A Deep Dive (CBOE Insights)](https://www.cboe.com/insights/posts/henry-schwartzs-zero-day-spx-iron-condor-strategy-a-deep-dive) —
  entry-after-volatility timing, 9:1 risk-reward per spread, 10-wide strikes
  example, gamma-risk warning near close.
- [Opening Range Breakout Trading Strategy (Option Alpha)](https://optionalpha.com/blog/opening-range-breakout-0dte-options-trading-strategy-explained) —
  60-minute opening range outperformed 30/15-minute variants in their
  backtest (89.4% win rate, profit factor 1.44, max drawdown -$3,453); trade
  selection rule (breakout up -> sell put spread, breakout down -> sell call
  spread) adopted directly into `modules/market_open.py` + `strategy_selector.py`.
- [0DTE Iron Condor: Why the Math Works Against You (FOTW/fattail.ai)](https://fattail.ai/0dte-iron-condor/) —
  the win-rate-vs-expectancy trap this whole system is built to report
  honestly rather than hide; direct match to your own Vajra fill-slippage
  finding.
- [Skewed Iron Condors Explained (HaiKhuu Trading)](https://haikhuu.com/education/skewed-iron-condor) —
  unbalanced/skewed IC construction for directional bias, basis for the
  "IIC" (skewed/unbalanced Iron Condor) structure in this system.
- Additional background (not individually fetched, titles reviewed):
  [10 Steps to Master the Iron Condor Strategy (Trasignal)](https://trasignal.com/blog/learn/iron-condor-strategy/),
  [After 9,000 trades, the 0DTE Breakeven Iron Condor... (Theta Profits)](https://www.thetaprofits.com/my-most-profitable-options-trading-strategy-0dte-breakeven-iron-condor/),
  [Iron Condor Strategy: Setup, Win Rate 65-70% (ApexVol)](https://apexvol.com/strategies/iron-condor),
  [0DTE SPX Iron Condor Strategy: Quantitative Analysis (SSRN via studylib)](https://studylib.net/doc/27926930/ultra-short-dated-option-spreads-as-a-fund-strategy--pearce-),
  [Regime-Conditional Alpha in SPY 0DTE Opening Range Breakout Strategies (SSRN, Justin Chuk)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6355218),
  [Iron Condor Strategy: When the Structure Supports the Range (fattail.ai)](https://fattail.ai/iron-condor-strategy/),
  [Options Blueprint Series: Iron Condors for Balanced Markets (TradingView)](https://pl.tradingview.com/chart/SI1%21/01M8Es4H-Options-Blueprint-Series-Iron-Condors-for-Balanced-Markets).

None of these publish a real 10-year, fill-adjusted, out-of-sample backtest.
The SSRN papers are the closest to rigorous and worth reading in full if you
want an academic take on regime-conditional 0DTE ORB strategies — I did not
have network access to fetch the full SSRN PDF from this sandbox.
