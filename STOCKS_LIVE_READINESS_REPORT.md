# US Stocks Strategy -- Live Readiness Report

Generated: 2026-09-03T20:45:00.077783+00:00

**PAPER TRADING ONLY. `STOCKS_LIVE_TRADING` is hard-set `False` in `src/stocks/config.py` and nothing in this project can change that programmatically. Reaching a `LIVE_CANDIDATE` verdict below is a statement about historical backtest quality -- it is NOT a decision to trade real money, and never triggers one. A human must explicitly review this report and separately decide whether, when, and how to ever enable live trading -- no code in this repository can do that on its own.**

## Summary

- Universe: 47 symbols
- Lookback: 3650 days (~10.0 years)
- Walk-forward folds: 5
- Total resolved historical trades (all strategies combined): **11073**
- Costs modeled: 5.0 bps slippage, $0.0 commission/trade (round-trip)

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
| breakout | ✓ | 1237 | 288 | 1.58 | 1.00 | 1.0 | ✗ |
| momentum | ✓ | 3662 | 1142 | 1.22 | 0.48 | 1.0 | ✗ |
| relative_volume | ✓ | 1901 | 553 | 1.14 | 0.33 | 0.8 | ✗ |
| pullback | ✓ | 3872 | 1074 | 1.05 | 0.12 | 0.8 | ✗ |
| mean_reversion | ✓ | 401 | 87 | 0.94 | -0.11 | 0.8 | ✗ |

## In-sample / Out-of-sample / Walk-forward detail (per strategy)

### momentum

- **Combined**: n=3662, win%=46.90, PF=1.24, expectancy%=0.50, Sharpe=0.08, Sortino=0.14, maxDD%=180.34
- **In-sample**: n=2520, win%=47.20, PF=1.24, expectancy%=0.50, Sharpe=0.08, Sortino=0.14, maxDD%=182.66
- **Out-of-sample**: n=1142, win%=46.30, PF=1.22, expectancy%=0.48, Sharpe=0.08, Sortino=0.14, maxDD%=95.80
- **Fold stability score**: 1.0 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - BULLISH_LOW: n=1510, expectancy%=0.68, PF=1.35
  - SIDEWAYS_LOW: n=1579, expectancy%=0.27, PF=1.13
  - BEARISH_LOW: n=32, expectancy%=0.90, PF=1.54
  - SIDEWAYS_HIGH: n=155, expectancy%=1.05, PF=1.44
  - BEARISH_HIGH: n=70, expectancy%=1.44, PF=1.55
  - BULLISH_HIGH: n=316, expectancy%=0.22, PF=1.08
- **Live-readiness verdict**: **LIVE_CANDIDATE**
  - ✓ enough_combined_trades: 3662 (threshold 20)
  - ✓ enough_out_of_sample_trades: 1142 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.48 (threshold 0)
  - ✓ out_of_sample_profit_factor_above_threshold: 1.22 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 10.08 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 1.00 (threshold 0.60)

### breakout

- **Combined**: n=1237, win%=47.00, PF=1.29, expectancy%=0.49, Sharpe=0.10, Sortino=0.18, maxDD%=95.35
- **In-sample**: n=949, win%=45.90, PF=1.20, expectancy%=0.34, Sharpe=0.07, Sortino=0.12, maxDD%=92.27
- **Out-of-sample**: n=288, win%=50.30, PF=1.58, expectancy%=1.00, Sharpe=0.18, Sortino=0.37, maxDD%=55.38
- **Fold stability score**: 1.0 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=619, expectancy%=0.35, PF=1.23
  - BULLISH_LOW: n=478, expectancy%=0.66, PF=1.40
  - BULLISH_HIGH: n=62, expectancy%=0.73, PF=1.31
  - BEARISH_HIGH: n=24, expectancy%=-0.74, PF=0.73
  - SIDEWAYS_HIGH: n=45, expectancy%=1.17, PF=1.53
- **Live-readiness verdict**: **LIVE_CANDIDATE**
  - ✓ enough_combined_trades: 1237 (threshold 20)
  - ✓ enough_out_of_sample_trades: 288 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 1.00 (threshold 0)
  - ✓ out_of_sample_profit_factor_above_threshold: 1.58 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 6.36 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 1.00 (threshold 0.60)

### mean_reversion

- **Combined**: n=401, win%=44.10, PF=1.15, expectancy%=0.24, Sharpe=0.06, Sortino=0.11, maxDD%=44.31
- **In-sample**: n=314, win%=45.20, PF=1.23, expectancy%=0.33, Sharpe=0.09, Sortino=0.16, maxDD%=39.07
- **Out-of-sample**: n=87, win%=40.20, PF=0.94, expectancy%=-0.11, Sharpe=-0.03, Sortino=-0.04, maxDD%=44.07
- **Fold stability score**: 0.8 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=243, expectancy%=0.23, PF=1.16
  - BEARISH_LOW: n=60, expectancy%=0.60, PF=1.42
  - BULLISH_LOW: n=31, expectancy%=-0.63, PF=0.66
  - BEARISH_HIGH: n=53, expectancy%=0.25, PF=1.14
  - SIDEWAYS_HIGH: n=13, expectancy%=0.03, PF=1.02
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 401 (threshold 20)
  - ✓ enough_out_of_sample_trades: 87 (threshold 10)
  - ✗ positive_out_of_sample_expectancy: -0.11 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 0.94 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 2.13 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.80 (threshold 0.60)

### pullback

- **Combined**: n=3872, win%=45.00, PF=1.15, expectancy%=0.28, Sharpe=0.06, Sortino=0.09, maxDD%=219.62
- **In-sample**: n=2798, win%=45.60, PF=1.20, expectancy%=0.34, Sharpe=0.07, Sortino=0.12, maxDD%=219.45
- **Out-of-sample**: n=1074, win%=43.50, PF=1.05, expectancy%=0.12, Sharpe=0.02, Sortino=0.03, maxDD%=100.50
- **Fold stability score**: 0.8 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=2238, expectancy%=0.17, PF=1.10
  - BEARISH_LOW: n=115, expectancy%=-0.36, PF=0.83
  - BULLISH_LOW: n=1014, expectancy%=0.54, PF=1.34
  - SIDEWAYS_HIGH: n=186, expectancy%=0.22, PF=1.09
  - BULLISH_HIGH: n=216, expectancy%=1.12, PF=1.50
  - BEARISH_HIGH: n=103, expectancy%=-0.93, PF=0.68
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 3872 (threshold 20)
  - ✓ enough_out_of_sample_trades: 1074 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.12 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 1.05 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 4.90 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.80 (threshold 0.60)

### relative_volume

- **Combined**: n=1901, win%=43.90, PF=1.14, expectancy%=0.36, Sharpe=0.05, Sortino=0.08, maxDD%=210.59
- **In-sample**: n=1348, win%=43.30, PF=1.14, expectancy%=0.37, Sharpe=0.05, Sortino=0.08, maxDD%=209.40
- **Out-of-sample**: n=553, win%=45.20, PF=1.14, expectancy%=0.33, Sharpe=0.05, Sortino=0.09, maxDD%=111.77
- **Fold stability score**: 0.8 (fraction of the 5 walk-forward folds where this strategy was both profitable and PF>1)
- **By market regime** (buckets with ≥10 trades only):
  - SIDEWAYS_LOW: n=889, expectancy%=0.44, PF=1.19
  - BULLISH_LOW: n=564, expectancy%=-0.07, PF=0.97
  - BEARISH_LOW: n=39, expectancy%=-0.45, PF=0.83
  - BEARISH_HIGH: n=230, expectancy%=1.28, PF=1.44
  - BULLISH_HIGH: n=107, expectancy%=0.32, PF=1.09
  - SIDEWAYS_HIGH: n=72, expectancy%=0.25, PF=1.08
- **Live-readiness verdict**: **NOT_READY**
  - ✓ enough_combined_trades: 1901 (threshold 20)
  - ✓ enough_out_of_sample_trades: 553 (threshold 10)
  - ✓ positive_out_of_sample_expectancy: 0.33 (threshold 0)
  - ✗ out_of_sample_profit_factor_above_threshold: 1.14 (threshold 1.15)
  - ✓ return_to_drawdown_ratio_above_threshold: 3.25 (threshold 2.00)
  - ✓ stable_across_walk_forward_folds: 0.80 (threshold 0.60)

## Parameter sensitivity check -- breakout

Re-backtested with alternate parameter values (see `scripts/research_stocks_strategies.py`'s `_PARAM_GRIDS`), ranked by cross-fold stability first, raw profit factor second -- a config that only wins because of one specific threshold, or only in one fold, is exactly what this check exists to surface rather than reward.

| Params | Trades | PF | Fold Stability |
|---|---|---|---|
| {'MIN_RELATIVE_VOLUME': 1.5, 'NEAR_HIGH_PCT_THRESHOLD': -1.0} | 2018 | 1.31 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.5, 'NEAR_HIGH_PCT_THRESHOLD': -0.5} | 1237 | 1.29 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.2, 'NEAR_HIGH_PCT_THRESHOLD': -1.0} | 3518 | 1.27 | 1.0 |
| {'MIN_RELATIVE_VOLUME': 1.2, 'NEAR_HIGH_PCT_THRESHOLD': -0.5} | 2324 | 1.23 | 0.8 |
| {'MIN_RELATIVE_VOLUME': 2.5, 'NEAR_HIGH_PCT_THRESHOLD': -1.0} | 466 | 1.23 | 0.8 |
| {'MIN_RELATIVE_VOLUME': 2.5, 'NEAR_HIGH_PCT_THRESHOLD': -0.5} | 267 | 1.18 | 0.8 |
| {'MIN_RELATIVE_VOLUME': 1.2, 'NEAR_HIGH_PCT_THRESHOLD': -0.25} | 1402 | 1.16 | 0.8 |
| {'MIN_RELATIVE_VOLUME': 2.0, 'NEAR_HIGH_PCT_THRESHOLD': -1.0} | 912 | 1.13 | 0.6 |

## Survivorship-bias disclosure

STOCKS_UNIVERSE is a fixed list of tickers that still exist today; any symbol that would have been delisted/failed/acquired during the lookback window is absent by construction. Results are somewhat optimistic relative to a full historical universe including failed companies.

## What still prevents live trading

- `STOCKS_LIVE_TRADING` is hard-set `False` at the source level -- there is no environment variable, config flag, or CLI argument anywhere in this project that can change it.
- No code path in `src/stocks` submits a real brokerage order; `src/stocks/alpaca_client.py` only ever calls Alpaca's **paper** endpoint.
- This report is historical-backtest evidence only. It has not been supplemented by weeks of live paper-trading volume (by design -- see the pipeline's own stated goal of using historical data for statistical sample size and live paper trading only to validate execution mechanics: data freshness, signal generation, order simulation, position tracking, stops/targets, restart recovery, duplicate-trade prevention -- see `tests/stocks/test_paper_broker.py`, `tests/stocks/test_engine.py`, and this project's live, continuously-running paper loop).
- A human has not yet reviewed and approved this specific report for a live-trading decision.
