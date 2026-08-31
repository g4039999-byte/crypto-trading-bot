"""Decision log for paper trading -- a separate file from
src/trade_logger.py (data/trade_log.jsonl) on purpose, so a simulated
run can never be confused with (or accidentally appear alongside) a real
one. Every entry is also tagged "mode": "PAPER" for extra clarity if the
two logs are ever compared side by side.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "paper_trade_log.jsonl"


def log_decision(action, symbol, token_address, reason, extra=None):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "PAPER",
        "action": action,
        "symbol": symbol,
        "token_address": token_address,
        "reason": reason,
    }
    if extra:
        entry.update(extra)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        logger.error("Failed to write paper trade log entry: %s", exc)

    logger.info("[PAPER %s] %s (%s): %s", action, symbol, token_address, reason)
    return entry
