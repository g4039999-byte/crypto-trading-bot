import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from src.config import SNAPSHOT_HISTORY_LIMIT
from src.utils import safe_get

logger = logging.getLogger(__name__)

# Resolved relative to the project root (parent of src/) so this works no
# matter what directory the radar is launched from, e.g. both
# `python -m src.radar` from the repo root and running tests from elsewhere.
SNAPSHOT_FILE = Path(__file__).resolve().parent.parent / "data" / "snapshots.json"


def _load_all():
    if not SNAPSHOT_FILE.exists():
        return {}

    try:
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read snapshot file (%s) -- starting fresh: %s", SNAPSHOT_FILE, exc)
        return {}


def save_snapshot(token_address, pair):
    """Append a snapshot for token_address, trimmed to the configured
    history limit. Failures are logged, never raised, so a snapshot
    write problem cannot bring down the rest of the radar run.
    """
    if not token_address or token_address == "?":
        logger.debug("Skipping snapshot save: no usable token address")
        return

    try:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _load_all()

        token_history = data.setdefault(token_address, [])

        pair = pair if isinstance(pair, dict) else {}

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price_usd": pair.get("priceUsd"),
            "liquidity_usd": safe_get(pair, "liquidity", "usd"),
            "volume_24h": safe_get(pair, "volume", "h24"),
            "buys_24h": safe_get(pair, "txns", "h24", "buys", default=0),
            "sells_24h": safe_get(pair, "txns", "h24", "sells", default=0),
        }

        token_history.append(snapshot)
        data[token_address] = token_history[-SNAPSHOT_HISTORY_LIMIT:]

        SNAPSHOT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, TypeError, AttributeError) as exc:
        logger.error("Failed to save snapshot for %s: %s", token_address, exc)


def load_snapshots(token_address):
    data = _load_all()
    return data.get(token_address, [])


def known_addresses(limit=None):
    """Every token address with at least one saved snapshot, most
    recently updated first. Used to build a "watchlist" so the radar
    keeps re-checking tokens it has already seen even once they drop out
    of DexScreener's "latest profiles" feed -- without this, most tokens
    would only ever get a single snapshot and observation.py would keep
    reporting INSUFFICIENT_DATA forever instead of a real trend.
    """
    data = _load_all()

    def last_seen(address):
        history = data.get(address) or []
        return history[-1].get("timestamp", "") if history else ""

    addresses = sorted(data.keys(), key=last_seen, reverse=True)
    if limit is not None:
        addresses = addresses[:limit]
    return addresses
