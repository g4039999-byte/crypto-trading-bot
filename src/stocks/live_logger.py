"""Audit log for every REAL stocks order decision/attempt -- its own
file (data/stocks/live_trade_log.jsonl), entirely separate from paper
trading's data/stocks/paper_trade_log.jsonl. Every entry is tagged
"mode": "LIVE" and "market": "US_STOCKS" so a log reviewer can never
mistake a live entry for a paper one, or vice versa. Every decision this
project's live path can reach -- BLOCKED (a gate refused), SKIP (risk
check declined), BUY/SELL (a real order was submitted), FILLED/REJECTED/
TIMEOUT/UNCONFIRMED/ERROR (the outcome) -- is logged here, never only
the successes. Never logs API keys, secrets, or raw request/response
bodies -- only the specific fields named by the caller's `extra` dict.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "live_trade_log.jsonl"


def log_decision(action, symbol, reason, extra=None):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "LIVE",
        "market": "US_STOCKS",
        "action": action,
        "symbol": symbol,
        "reason": reason,
    }
    if extra:
        entry.update(extra)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("Failed to write stocks LIVE trade log entry: %s", exc)

    logger.warning("[STOCKS LIVE %s] %s: %s", action, symbol, reason)
    return entry
