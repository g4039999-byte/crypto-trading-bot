"""Strategy versioning for the stocks subsystem -- mirrors scripts/
strategy_registry.py on the crypto side, adapted: here a "version" is
which strategy is currently the active one (STOCKS_ACTIVE_STRATEGY),
backed by a real backtest result recorded before adoption, with a
clear rollback path if a later version underperforms.

data/stocks/strategy_versions.json is the append-only history (never
edited by hand). Never touches paper trading state.

CLI:
    python -m src.stocks.strategy_registry list
    python -m src.stocks.strategy_registry record momentum --rationale "..."
    python -m src.stocks.strategy_registry activate momentum
"""

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.stocks.benchmarks import buy_and_hold
from src.stocks.backtester import backtest_strategy
from src.stocks.config import BACKTEST_LOOKBACK_DAYS, BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE, STOCKS_UNIVERSE
from src.stocks.performance import compute_metrics
from src.stocks.strategies import STRATEGIES

logger = logging.getLogger(__name__)

REGISTRY_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "strategy_versions.json"
ACTIVE_STRATEGY_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "active_strategy.json"

DAILY_STRATEGIES = tuple(name for name, mod in STRATEGIES.items() if getattr(mod, "TIMEFRAME", "daily") == "daily")


def _load_registry():
    if not REGISTRY_FILE.exists():
        return {"versions": []}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"versions": []}


def _save_registry(registry):
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def record_version(strategy_name, rationale=""):
    """Backtest `strategy_name` against the current universe/lookback
    and append the result (including the buy-and-hold benchmark on the
    same run, for an apples-to-apples comparison) to the registry.
    Never auto-adopts -- call activate_strategy() separately.
    """
    if strategy_name not in DAILY_STRATEGIES:
        raise KeyError(f"{strategy_name!r} is not a daily-bar strategy (available: {DAILY_STRATEGIES})")

    symbols = list(STOCKS_UNIVERSE)
    trades = backtest_strategy(strategy_name, symbols, BACKTEST_LOOKBACK_DAYS)
    out_of_sample = [t.pnl_pct for t in trades if not t.in_sample]
    combined = [t.pnl_pct for t in trades]

    benchmark_pnls = buy_and_hold(symbols, BACKTEST_LOOKBACK_DAYS)

    entry = {
        "strategy": strategy_name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "rationale": rationale,
        "lookback_days": BACKTEST_LOOKBACK_DAYS,
        "universe_size": len(symbols),
        "combined": compute_metrics(combined),
        "out_of_sample": compute_metrics(out_of_sample),
        "benchmark_buy_and_hold": compute_metrics(benchmark_pnls),
        "statistically_significant": len(combined) >= BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE,
    }

    registry = _load_registry()
    registry["versions"].append(entry)
    _save_registry(registry)
    return entry


def list_versions():
    return _load_registry()["versions"]


def get_active_strategy():
    """The currently-adopted strategy name, or None if none has been
    explicitly activated yet (src.stocks.engine falls back to running
    every strategy and picking the best-confidence signal per candidate
    in that case -- see src.stocks.scoring.best_strategy_signal).
    """
    if not ACTIVE_STRATEGY_FILE.exists():
        return None
    try:
        data = json.loads(ACTIVE_STRATEGY_FILE.read_text(encoding="utf-8"))
        return data.get("strategy")
    except (json.JSONDecodeError, OSError):
        return None


def activate_strategy(strategy_name):
    """Adopt `strategy_name` as the one active strategy engine.py will
    prefer. Recorded with its own timestamp so a later rollback
    (activate_strategy() with a previous name) has a clear history of
    what was active when.
    """
    if strategy_name not in DAILY_STRATEGIES and strategy_name is not None:
        raise KeyError(f"{strategy_name!r} is not a known strategy")

    ACTIVE_STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_STRATEGY_FILE.write_text(json.dumps({
        "strategy": strategy_name, "activated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")
    logger.info("Stocks active strategy set to %r", strategy_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    record_parser = sub.add_parser("record")
    record_parser.add_argument("strategy", choices=DAILY_STRATEGIES)
    record_parser.add_argument("--rationale", default="")

    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("strategy", choices=list(DAILY_STRATEGIES) + ["none"])

    args = parser.parse_args()

    if args.command == "list":
        versions = list_versions()
        active = get_active_strategy()
        print(f"Active strategy: {active or '(none -- engine picks best-confidence signal per candidate)'}\n")
        for v in versions:
            c = v["combined"]
            b = v["benchmark_buy_and_hold"]
            print(
                f"{v['recorded_at']}  {v['strategy']:<15} "
                f"n={c['trade_count']:<4} win%={c['win_rate_pct']} total_ret%={c['total_return_pct']} "
                f"(buy&hold total_ret%={b['total_return_pct']})  significant={v['statistically_significant']}  -- {v['rationale']}"
            )
    elif args.command == "record":
        entry = record_version(args.strategy, args.rationale)
        print(json.dumps(entry, indent=2))
    elif args.command == "activate":
        activate_strategy(None if args.strategy == "none" else args.strategy)
        print(f"Activated: {args.strategy}")


if __name__ == "__main__":
    main()
