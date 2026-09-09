# 0DTE SPX Intraday Multi-Window Backtest Report

Generated: 2026-09-08T20:00:23.178537

## Daily price data: user-supplied CSV (real SPX/VIX)

SPX file: `/root/.claude/uploads/9d90db84-c2af-5bd2-a9bc-9739c6b18162/0feab6a4-spx_daily.csv`  
VIX file: `/root/.claude/uploads/9d90db84-c2af-5bd2-a9bc-9739c6b18162/a5a58324-vix_daily.csv`

## ⚠️ INTRADAY PATH: SIMULATED, constrained to match real daily OHLC

No real intraday (minute-level) history was available for the full backtest window (see README §6/7f) -- every day's path is a simulated 4-anchor bridge (open -> low/high in an assumed order -> close) with Brownian-bridge noise, clipped to the day's real high/low. This is what lets lot-level profit-target/stop/strike-test exits be tested at all, but exact exit TIMING and whether a specific intraday touch actually happened is a model assumption, not observed fact. See `modules/intraday_path.py` for the exact method.

## Summary statistics (per LOT, not per day -- a day can contribute up to 9 lots across 3 windows)

- **n_lots**: 22491
- **n_signals**: 7497
- **win_rate_pct**: 59.0
- **avg_win**: 220.93
- **avg_loss**: -317.25
- **expectancy_per_lot**: 0.17
- **total_pnl**: 3759.47
- **profit_factor**: 1.0
- **max_drawdown**: -359226.48
- **exit_reason_breakdown**: {'profit_target': 10127, 'stop_loss': 9248, 'eod_close': 2734, 'strike_tested': 382}
- **structure_breakdown**: {'balanced_iron_condor': 18471, 'skewed_unbalanced_iron_condor': 2217, 'put_credit_spread': 978, 'call_credit_spread': 825}
- **window_breakdown**: {'afternoon': {'count': 7452, 'sum': -412004.15, 'mean': -55.29}, 'midday': {'count': 7503, 'sum': -234562.02, 'mean': -31.26}, 'morning': {'count': 7536, 'sum': 650325.64, 'mean': 86.3}}

## Reminder

This models the STACKING design (multiple concurrent windows, no cap by default) -- compare n_signals here to the single-signal run_backtest.py result to see the actual trade-count multiplier, and compare total_pnl and max_drawdown directly against that run's numbers, not just win rate.
