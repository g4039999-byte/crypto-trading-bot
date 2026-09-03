"""Data-driven paper-trading strategy versioning: record a named
parameter set together with its backtest result (scripts/
backtest_paper_strategy.py, replayed against real historical snapshot
data), list what's on record, and activate any recorded version by
writing its PAPER_* values into .env -- which src/config.py already
reads on the next process start. This is the persistent half of:

    Paper Trading -> collect results -> analyze -> adjust -> test
    -> compare -> adopt the best -> (able to roll back)

data/strategy_versions.json is the append-only history (never edited by
hand -- always through record_version()/CLI below). Never touches
data/paper_positions.json or data/paper_trade_log.jsonl, and never
writes anything live-trading-related.

CLI:
    python -m scripts.strategy_registry list
    python -m scripts.strategy_registry record <name> --rationale "..."
    python -m scripts.strategy_registry activate <name>
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backtest_paper_strategy import CANDIDATE, CURRENT, Strategy, _load_snapshots, run_backtest  # noqa: E402

REGISTRY_FILE = PROJECT_ROOT / "data" / "strategy_versions.json"
ENV_FILE = PROJECT_ROOT / ".env"

# Every PAPER_* env var name a Strategy maps to -- keeps record()/
# activate() in sync with src/config.py's actual override keys.
PAPER_ENV_KEYS = {
    "min_score": "PAPER_MIN_SCORE",
    "entry_trends": "PAPER_ENTRY_TRENDS",
    "min_liquidity_usd": "PAPER_MIN_LIQUIDITY_USD",
    "min_volume_24h_usd": "PAPER_MIN_VOLUME_24H_USD",
    "min_age_minutes": "PAPER_MIN_PAIR_AGE_MINUTES",
    "max_age_minutes": "PAPER_MAX_PAIR_AGE_MINUTES",
    "stop_loss_pct": "PAPER_STOP_LOSS_PCT",
    "take_profit_pct": "PAPER_TAKE_PROFIT_PCT",
    # max_holding_minutes deliberately excluded: it maps to the shared
    # MAX_HOLDING_MINUTES (also used by live's src/portfolio.py) --
    # this tool only ever writes PAPER_*-prefixed, paper-only keys, so
    # activating any preset here can never change live's configuration.
    "max_liq_drawdown_pct": "PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT",
    "stop_loss_cooldown_minutes": "PAPER_STOP_LOSS_COOLDOWN_MINUTES",
}

# Named presets available to record/activate. "v1_legacy" is what
# shipped before the 2026-09-03 diagnosis (see src/config.py's PAPER_*
# comments); "v2_active" is the current default. Add a new named
# Strategy here for any future candidate instead of only editing
# src/config.py directly, so it stays comparable and reversible.
PRESETS = {
    "v1_legacy": Strategy(
        name="v1_legacy", min_score=80, entry_trends=("STRONG", "RISING"),
        min_liquidity_usd=15000, min_volume_24h_usd=50000,
        min_age_minutes=5, max_age_minutes=180,
        stop_loss_pct=25, take_profit_pct=50, max_holding_minutes=240,
        max_liq_drawdown_pct=None, stop_loss_cooldown_minutes=0,
    ),
    "v2_active": CANDIDATE,
}


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


def _backtest_summary(strategy):
    snapshots = _load_snapshots()
    trades = run_backtest(strategy, snapshots)
    n = len(trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    return {
        "tokens_replayed": len(snapshots),
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate_pct": round(100 * len(wins) / n, 1) if n else None,
        "total_pnl_usd": round(sum(t.pnl_usd for t in trades), 2),
    }


def record_version(name, rationale=""):
    """Backtest `name` (must be in PRESETS) against current historical
    data and append the result to the registry. Never adopted/activated
    automatically -- call activate_version() separately once you've
    compared it to what's on record.
    """
    if name not in PRESETS:
        raise KeyError(f"Unknown preset {name!r} -- add it to PRESETS in this file first")

    strategy = PRESETS[name]
    result = _backtest_summary(strategy)
    params = {field: value for field, value in asdict(strategy).items() if field != "name"}

    registry = _load_registry()
    registry["versions"].append({
        "name": name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "rationale": rationale,
        "params": params,
        "backtest": result,
    })
    _save_registry(registry)
    return result


def list_versions():
    return _load_registry()["versions"]


def activate_version(name):
    """Write `name`'s PAPER_* values into .env (creating it if needed,
    preserving every other line/value already there) so the *next*
    `python -m webapp.app` / `python -m src.radar` picks them up --
    src/config.py already reads every one of these from the
    environment. This is the rollback path: activating an older
    recorded version is exactly this, no code change required.
    """
    if name not in PRESETS:
        raise KeyError(f"Unknown preset {name!r} -- add it to PRESETS in this file first")

    strategy = PRESETS[name]
    overrides = {}
    for field, env_key in PAPER_ENV_KEYS.items():
        value = getattr(strategy, field)
        if value is None:
            continue  # None means "use src/config.py's own default" -- do not force an override
        if field == "entry_trends":
            value = ",".join(value)
        overrides[env_key] = str(value)

    existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    written = set()
    new_lines = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in overrides:
            new_lines.append(f"{key}={overrides[key]}")
            written.add(key)
        else:
            new_lines.append(line)
    for key, value in overrides.items():
        if key not in written:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return overrides


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    record_parser = sub.add_parser("record")
    record_parser.add_argument("name", choices=sorted(PRESETS))
    record_parser.add_argument("--rationale", default="")

    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("name", choices=sorted(PRESETS))

    args = parser.parse_args()

    if args.command == "list":
        versions = list_versions()
        if not versions:
            print("No versions recorded yet -- run: python -m scripts.strategy_registry record <name>")
        for v in versions:
            b = v["backtest"]
            print(
                f"{v['recorded_at']}  {v['name']:<12} "
                f"trades={b['trades']:<3} win_rate={b['win_rate_pct']}%  "
                f"total_pnl=${b['total_pnl_usd']:+.2f}  -- {v['rationale']}"
            )
    elif args.command == "record":
        result = record_version(args.name, args.rationale)
        print(f"Recorded {args.name}: {result}")
    elif args.command == "activate":
        overrides = activate_version(args.name)
        print(f"Wrote {len(overrides)} PAPER_* override(s) to {ENV_FILE} for {args.name}:")
        for k, v in overrides.items():
            print(f"  {k}={v}")
        print("Restart the radar/webapp process for this to take effect.")


if __name__ == "__main__":
    main()
