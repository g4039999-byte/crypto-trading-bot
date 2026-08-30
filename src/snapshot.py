import json
from pathlib import Path
from datetime import datetime, timezone


SNAPSHOT_FILE = Path("data/snapshots.json")


def save_snapshot(token_address, pair):
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if SNAPSHOT_FILE.exists():
        try:
            data = json.loads(
                SNAPSHOT_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}

    token_history = data.setdefault(token_address, [])

    txns = pair.get("txns", {}).get("h24", {})

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": pair.get("liquidity", {}).get("usd"),
        "volume_24h": pair.get("volume", {}).get("h24"),
        "buys_24h": txns.get("buys", 0),
        "sells_24h": txns.get("sells", 0),
    }

    token_history.append(snapshot)

    # Keep only the latest 60 snapshots per token.
    data[token_address] = token_history[-60:]

    SNAPSHOT_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def load_snapshots(token_address):
    if not SNAPSHOT_FILE.exists():
        return []

    try:
        data = json.loads(
            SNAPSHOT_FILE.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        return []

    return data.get(token_address, [])