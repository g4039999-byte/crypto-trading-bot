# US Stocks Strategy -- Live Readiness Report

Generated: 2026-09-03T23:45:09.135223+00:00

**PAPER TRADING ONLY. `STOCKS_LIVE_TRADING` is hard-set `False` in `src/stocks/config.py` and nothing in this project can change that programmatically. Reaching a `LIVE_CANDIDATE` verdict below is a statement about historical backtest quality -- it is NOT a decision to trade real money, and never triggers one. A human must explicitly review this report and separately decide whether, when, and how to ever enable live trading -- no code in this repository can do that on its own.**

## Summary

- Universe: 47 symbols
- Lookback: 3650 days (~10.0 years)
- Walk-forward folds: 5
- Total resolved historical trades (all strategies combined): **10896**
- Costs modeled: 5.0 bps slippage, $0.0 commission/trade (round-trip)
- Ranked #1 by this run's ranking (significance, then fold-stability, then out-of-sample expectancy/PF): **momentum**. This is NOT necessarily the strategy actually active in paper trading -- see `python -m src.stocks.strategy_registry list` for the live active strategy and the specific rationale recorded for it (a higher fold-stability score alone doesn't always outweigh a substantially higher per-trade edge; see that rationale for the actual reasoning behind whichever strategy is active).

## Baselines

| Baseline | Trades | Win% | PF | Expectancy% | Sharpe | Sortino | MaxDD% |
|---|---|---|---|---|---|---|---|
| buy_and_hold | 47 | 95.70 | 1223.30 | 3422.47 | 0.34 | 236.29 | 131.60 |
| simple_momentum | 3561 | 57.30 | 1.43 | 0.97 | 0.12 | 0.20 | 168.04 |
| simple_breakout | 5012 | 57.50 | 1.50 | 0.97 | 0.13 | 0.23 | 199.02 |
| simple_volume | 3652 | 55.30 | 1.49 | 1.21 | 0.10 | 0.22 | 243.20 |

## Strategy ranking

| Strategy | Significant | Combined N | OOS N | OOS PF | OOS Expectancy% | Fold Stability | Beats B&H (Sharpe) |
|---|---|---|---|---|---|---|---|
| momentum | ✓ | 3545 | 1102 | 1.29 | 0.64 | 1.0 | ✗ |
| pullback | ✓ | 3826 | 1063 | 1.08 | 0.16 | 0.8 | ✗ |
| mean_reversion | ✓ | 401 | 87 | 1.02 | 0.05 | 0.8 | ✗ |
| breakout | ✓ | 1230 | 286 | 1.66 | 1.13 | 0.6 | ✗ |
| relative_volume | ✓ | 1894 | 551 | 1.07 | 0.16 | 0.6 | ✗ |

## In-sample / Out-of-sample / Walk-forward detail (per strategy)

### momentum

- **Combined**: n=3545, win%=45.50, PF=1.24, expectancy%=0.51, Sharpe=0.08, Sortino=0.14, maxDD%=202.22
- **In-sample**: n=2443, win%=45.70, PF=1.22, expectancy%=0.45, Sharpe=0.07, Sortino=0.13, maxDD%=203.95
- **Out-of-sample**: n=1102, win%=44.90, PF=1.29, expectancy%=0.64, Sharpe=0.09, Sortino=0.18, maxDD%=86.35
- **Fold stability score**: 1.0 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - BULLISH_LOW: n=1462, expectancy%=0.77, PF=1.40
  - SIDEWAYS_LOW: n=1532, expectancy%=0.31, PF=1.15
  - BULLISH_HIGH: n=311, expectancy%=-0.03, PF=0.99
  - SIDEWAYS_HIGH: n=143, expectancy%=0.77, PF=1.33
  - BEARISH_LOW: n=33, expectancy%=0.01, PF=1.00
  - BEARISH_HIGH: n=64, expectancy%=1.43, PF=1.58
- **Live-readiness verdict**: **LIVE_CANDIDATE**
  - ✓ enough_combined_trades: 3545 (threshold 20)
  - ✓ enough_out_of_sample_trades: 1102 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.64 (threshold 0)
  - ✓ out_of_sample_profit_factor_above_threshold: 1.29 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 8.87 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 1.00 (threshold 0.60)

### breakout

- **Combined**: n=1230, win%=44.90, PF=1.25, expectancy%=0.41, Sharpe=0.08, Sortino=0.15, maxDD%=86.80
- **In-sample**: n=944, win%=43.90, PF=1.12, expectancy%=0.19, Sharpe=0.04, Sortino=0.07, maxDD%=83.04
- **Out-of-sample**: n=286, win%=48.30, PF=1.66, expectancy%=1.13, Sharpe=0.18, Sortino=0.42, maxDD%=66.10
- **Fold stability score**: 0.6 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=617, expectancy%=0.29, PF=1.19
  - BULLISH_LOW: n=473, expectancy%=0.50, PF=1.30
  - BULLISH_HIGH: n=62, expectancy%=1.05, PF=1.45
  - BEARISH_HIGH: n=24, expectancy%=-0.98, PF=0.65
  - SIDEWAYS_HIGH: n=45, expectancy%=1.30, PF=1.59
- **Live-readiness verdict**: **LIVE_CANDIDATE**
  - ✓ enough_combined_trades: 1230 (threshold 20)
  - ✓ enough_out_of_sample_trades: 286 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 1.13 (threshold 0)
  - ✓ out_of_sample_profit_factor_above_threshold: 1.66 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 5.84 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.60 (threshold 0.60)

### mean_reversion

- **Combined**: n=401, win%=41.60, PF=1.13, expectancy%=0.20, Sharpe=0.05, Sortino=0.09, maxDD%=54.40
- **In-sample**: n=314, win%=42.40, PF=1.17, expectancy%=0.25, Sharpe=0.07, Sortino=0.12, maxDD%=41.61
- **Out-of-sample**: n=87, win%=39.10, PF=1.02, expectancy%=0.05, Sharpe=0.01, Sortino=0.02, maxDD%=43.05
- **Fold stability score**: 0.8 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=242, expectancy%=0.18, PF=1.12
  - BEARISH_LOW: n=60, expectancy%=0.63, PF=1.46
  - BULLISH_LOW: n=31, expectancy%=-0.57, PF=0.68
  - BEARISH_HIGH: n=53, expectancy%=0.25, PF=1.14
  - SIDEWAYS_HIGH: n=14, expectancy%=-0.21, PF=0.86
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 401 (threshold 20)
  - ✓ enough_out_of_sample_trades: 87 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.05 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 1.02 (threshold 1.15)
  - ✗ return_to_drawdown_ratio_above_threshold: 1.49 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.80 (threshold 0.60)

### pullback

- **Combined**: n=3826, win%=43.60, PF=1.19, expectancy%=0.35, Sharpe=0.06, Sortino=0.12, maxDD%=137.42
- **In-sample**: n=2763, win%=44.00, PF=1.25, expectancy%=0.43, Sharpe=0.08, Sortino=0.16, maxDD%=119.31
- **Out-of-sample**: n=1063, win%=42.60, PF=1.08, expectancy%=0.16, Sharpe=0.03, Sortino=0.05, maxDD%=100.51
- **Fold stability score**: 0.8 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=2209, expectancy%=0.27, PF=1.16
  - BULLISH_LOW: n=1005, expectancy%=0.58, PF=1.36
  - BEARISH_LOW: n=113, expectancy%=-0.21, PF=0.90
  - SIDEWAYS_HIGH: n=185, expectancy%=0.27, PF=1.11
  - BULLISH_HIGH: n=212, expectancy%=1.16, PF=1.52
  - BEARISH_HIGH: n=102, expectancy%=-1.02, PF=0.65
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 3826 (threshold 20)
  - ✓ enough_out_of_sample_trades: 1063 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.16 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 1.08 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 9.85 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.80 (threshold 0.60)

### relative_volume

- **Combined**: n=1894, win%=42.30, PF=1.09, expectancy%=0.22, Sharpe=0.03, Sortino=0.05, maxDD%=300.59
- **In-sample**: n=1343, win%=41.70, PF=1.09, expectancy%=0.24, Sharpe=0.03, Sortino=0.05, maxDD%=294.76
- **Out-of-sample**: n=551, win%=43.70, PF=1.07, expectancy%=0.16, Sharpe=0.03, Sortino=0.04, maxDD%=126.47
- **Fold stability score**: 0.6 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - BEARISH_LOW: n=39, expectancy%=-0.06, PF=0.98
  - SIDEWAYS_LOW: n=886, expectancy%=0.36, PF=1.15
  - BEARISH_HIGH: n=230, expectancy%=1.25, PF=1.44
  - BULLISH_LOW: n=562, expectancy%=-0.36, PF=0.85
  - BULLISH_HIGH: n=105, expectancy%=0.89, PF=1.27
  - SIDEWAYS_HIGH: n=72, expectancy%=-1.20, PF=0.65
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 1894 (threshold 20)
  - ✓ enough_out_of_sample_trades: 551 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.16 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 1.07 (threshold 1.15)
  - ✗ return_to_drawdown_ratio_above_threshold: 1.36 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.60 (threshold 0.60)

## Parameter sensitivity check -- momentum

Re-backtested with alternate parameter values (see `scripts/research_stocks_strategies.py`'s `_PARAM_GRIDS`), ranked by cross-fold stability first, raw profit factor second -- a config that only wins because of one specific threshold, or only in one fold, is exactly what this check exists to surface rather than reward.

| Params | Trades | PF | Fold Stability |
|---|---|---|---|
| {'MIN_RELATIVE_VOLUME': 1.0, 'MAX_RSI': 72.0} | 3650 | 1.27 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.0, 'MAX_RSI': 78.0} | 4107 | 1.25 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.0, 'MAX_RSI': 85.0} | 4281 | 1.24 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.1, 'MAX_RSI': 78.0} | 3545 | 1.24 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.3, 'MAX_RSI': 78.0} | 2627 | 1.24 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.3, 'MAX_RSI': 85.0} | 2794 | 1.24 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.1, 'MAX_RSI': 85.0} | 3727 | 1.22 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.3, 'MAX_RSI': 72.0} | 2178 | 1.27 | 0.8 |

## Survivorship-bias disclosure

STOCKS_UNIVERSE is a fixed list of tickers that still exist today; any symbol that would have been delisted/failed/acquired during the lookback window is absent by construction. Results are somewhat optimistic relative to a full historical universe including failed companies.

## What still prevents live trading

- `STOCKS_LIVE_TRADING` is hard-set `False` at the source level -- there is no environment variable, config flag, or CLI argument anywhere in this project that can change it.
- No code path in `src/stocks` submits a real brokerage order; `src/stocks/alpaca_client.py` only ever calls Alpaca's **paper** endpoint.
- This report is historical-backtest evidence only. It has not been supplemented by weeks of live paper-trading volume (by design -- see the pipeline's own stated goal of using historical data for statistical sample size and live paper trading only to validate execution mechanics: data freshness, signal generation, order simulation, position tracking, stops/targets, restart recovery, duplicate-trade prevention -- see `tests/stocks/test_paper_broker.py`, `tests/stocks/test_engine.py`, and this project's live, continuously-running paper loop).
- A human has not yet reviewed and approved this specific report for a live-trading decision.
