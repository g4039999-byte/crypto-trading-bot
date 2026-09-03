"""Backtest every daily-bar stock strategy against the configured
universe, walk-forward split (in-sample vs out-of-sample), and compare
all of them to the baselines (buy & hold, naive momentum, naive volume
spike) -- this is how a strategy earns adoption in src/stocks: it has
to actually beat these on real historical data, not just have a
positive number on its own.

Usage:
    python -m scripts.backtest_stocks_strategies
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.stocks import benchmarks  # noqa: E402
from src.stocks.backtester import backtest_all_strategies  # noqa: E402
from src.stocks.config import BACKTEST_LOOKBACK_DAYS, STOCKS_UNIVERSE  # noqa: E402
from src.stocks.performance import compute_metrics  # noqa: E402


def _print_row(name, metrics):
    tc = metrics["trade_count"]
    if tc == 0:
        print(f"{name:<28} 0 trades")
        return
    print(
        f"{name:<28} n={tc:<4} win%={metrics['win_rate_pct']:<6} "
        f"total_ret%={metrics['total_return_pct']:<8} avg_win%={metrics['avg_win_pct']} "
        f"avg_loss%={metrics['avg_loss_pct']} PF={metrics['profit_factor']} "
        f"expectancy%={metrics['expectancy_pct']} sharpe={metrics['sharpe']} "
        f"sortino={metrics['sortino']} maxDD%={metrics['max_drawdown_pct']}"
    )


def main():
    symbols = list(STOCKS_UNIVERSE)
    print(f"=== Backtesting {len(symbols)} symbols, {BACKTEST_LOOKBACK_DAYS} lookback days ===\n")

    print("--- Baselines ---")
    bh = benchmarks.buy_and_hold(symbols, BACKTEST_LOOKBACK_DAYS)
    _print_row("buy_and_hold", compute_metrics(bh))
    mom_base = benchmarks.simple_momentum_baseline(symbols, BACKTEST_LOOKBACK_DAYS)
    _print_row("simple_momentum_baseline", compute_metrics(mom_base))
    vol_base = benchmarks.simple_volume_baseline(symbols, BACKTEST_LOOKBACK_DAYS)
    _print_row("simple_volume_baseline", compute_metrics(vol_base))

    print("\n--- Candidate strategies (out-of-sample only) ---")
    all_results = backtest_all_strategies(symbols, BACKTEST_LOOKBACK_DAYS)
    for name, trades in all_results.items():
        in_sample = [t.pnl_pct for t in trades if t.in_sample]
        out_sample = [t.pnl_pct for t in trades if not t.in_sample]
        print(f"\n{name}:")
        _print_row("  in-sample", compute_metrics(in_sample))
        _print_row("  out-of-sample", compute_metrics(out_sample))
        _print_row("  combined", compute_metrics(in_sample + out_sample))

    return all_results


if __name__ == "__main__":
    main()
