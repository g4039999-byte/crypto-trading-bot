"""Human-auditable log of every trading decision, one JSON object per
line, appended to data/trade_log.jsonl.

This is separate from the operational logging in src/logging_config.py
(which is for debugging the program) -- this file exists so that, at any
point, a person can open it and see exactly what the bot considered,
decided, and why, in order. It never contains secrets: no private key,
no seed phrase, no raw wallet signature.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path(__file__).resolve().parent.parent / "data" / "trade_log.jsonl"


def log_decision(action, symbol, token_address, reason, extra=None):
    """action: one of "BUY", "SKIP", "SELL", "BLOCKED".
    reason: short human-readable explanation.
    extra: optional dict of additional fields (score, price, size_usd, ...).
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
        logger.error("Failed to write trade log entry: %s", exc)

    logger.info("[%s] %s (%s): %s", action, symbol, token_address, reason)
    return entry
