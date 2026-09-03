"""CLI entry point for the US stocks paper-trading engine.

    python -m src.stocks.run                          # one cycle
    python -m src.stocks.run --loop                    # continuous, every STOCKS_LOOP_INTERVAL_SECONDS
    python -m src.stocks.run --loop --interval 60       # override the interval
    python -m src.stocks.run --loop --max-iterations 3  # bounded, for a demo/test

Paper trading only -- there is no --live flag, and none is planned
without a much larger, separate, explicitly-requested change (see
src/stocks/config.py's STOCKS_LIVE_TRADING, hard-set False at the
source level).
"""

import argparse
import logging

from src.logging_config import setup_logging  # reused as-is -- already generic, not crypto-specific
from src.stocks.engine import run_cycle, run_forever

logger = logging.getLogger(__name__)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="US stocks paper-trading engine (paper only)")
    parser.add_argument("--loop", action="store_true", help="run continuously instead of once")
    parser.add_argument("--interval", type=float, default=None, help="seconds between cycles in --loop mode")
    parser.add_argument("--max-iterations", type=int, default=None, help="stop --loop after N cycles (mainly for testing)")
    return parser.parse_args(argv)


def main(argv=None):
    setup_logging()
    args = _parse_args(argv)

    if args.loop:
        run_forever(interval_seconds=args.interval, max_iterations=args.max_iterations)
    else:
        logger.info("Starting stocks paper-trading run (one cycle)")
        run_cycle()


if __name__ == "__main__":
    main()
