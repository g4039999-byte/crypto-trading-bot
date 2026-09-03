"""Decision log for the stocks paper-trading engine -- its own file
(data/stocks/paper_trade_log.jsonl), entirely separate from the crypto
side's data/paper_trade_log.jsonl. Every entry is tagged "mode": "PAPER"
and "market": "US_STOCKS" for clarity if logs are ever compared.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "paper_trade_log.jsonl"


def log_decision(action, symbol, reason, extra=None):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER",
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
        logger.error("Failed to write stocks paper trade log entry: %s", exc)

    logger.info("[STOCKS PAPER %s] %s: %s", action, symbol, reason)
    return entry
