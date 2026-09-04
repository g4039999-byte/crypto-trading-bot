"""Read-only monitoring/analysis CLI for the radar, opportunity
watchlist, news signal engine, and paper-trading performance.

Run with: python -m src.cli <command> [options]

This module is STRICTLY read-only: it never buys, sells, executes a
trade, touches a wallet, signs a transaction, or handles a key, and it
never writes to any state file -- every command only calls existing
read-only functions from src.opportunity_watchlist, src.news_signal_engine,
and src.performance_analyzer, and reads a few plain config values.

Isolation (explicit requirement for this phase): this file is never
imported by, and NEVER imports, src/wallet.py, src/risk.py,
src/live_trader.py, or src/paper_trader.py, and does not import
src/radar.py either -- see tests/test_cli.py's isolation test. The one
execution-safety value this module displays (EXECUTION_ENABLED_IN_CODE,
in the `status` command) is read by parsing wallet.py's own source text
as a plain string (see _read_execution_enabled_flag() below) rather
than importing the wallet module -- this file has zero Python-level
dependency on src.wallet, even for that one display value.
"""

import argparse
import os
import re
from pathlib import Path

from src.news_signal_engine import active_signals, signals_for_symbols
from src.opportunity_watchlist import NEWS_SIGNAL_WATCHLIST_LINK_ENABLED, get_opportunity, list_all, list_by_status
from src.performance_analyzer import analyze_recent
from src.pumpfun_client import is_configured as pumpfun_is_configured

_WALLET_SOURCE_PATH = Path(__file__).resolve().parent / "wallet.py"

_STATUSES = ("NEW", "WATCHING", "QUALIFIED", "REJECTED", "EXPIRED")


def _pct(value):
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _usd(value):
    return "N/A" if value is None else f"${value:,.2f}"


def _read_execution_enabled_flag():
    """Reads wallet.py's EXECUTION_ENABLED_IN_CODE value as plain text
    for display in the `status` command -- deliberately without ever
    importing the wallet module itself, so this file keeps zero
    Python-level dependency on src/wallet.py even for this one value.
    Returns the flag's literal source text ("False"/"True") or a clear
    "unknown" string if the line can't be found or the file can't be
    read -- never raises.
    """
    try:
        text = _WALLET_SOURCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return "unknown (could not read wallet.py)"
    match = re.search(r"^EXECUTION_ENABLED_IN_CODE\s*=\s*(\w+)", text, re.MULTILINE)
    return match.group(1) if match else "unknown (line not found)"


# ---------------------------------------------------------------------------
# watchlist
# ---------------------------------------------------------------------------

def _cmd_watchlist(args):
    entries = list_by_status(args.status) if args.status else list_all()

    if not entries:
        scope = f"in status {args.status}" if args.status else "yet"
        print(f"No opportunities tracked {scope}.")
        return

    print(f"{'SYMBOL':<12} {'STATUS':<10} {'SCORE':>6} {'NEWS':>5}  ADDRESS")
    print("-" * 70)
    for entry in entries:
        history = entry.get("history") or []
        latest_score = history[-1].get("score") if history else None
        score_text = "N/A" if latest_score is None else str(latest_score)
        news_count = len(entry.get("news") or [])
        print(
            f"{str(entry.get('symbol') or '?'):<12} {str(entry.get('status') or '?'):<10} "
            f"{score_text:>6} {news_count:>5}  {entry.get('address', '?')}"
        )
    print(f"\n{len(entries)} opportunity(ies) shown.")


# ---------------------------------------------------------------------------
# opportunity <address>
# ---------------------------------------------------------------------------

def _cmd_opportunity(args):
    entry = get_opportunity(args.address)
    if entry is None:
        print(f"No opportunity tracked for address: {args.address}")
        return

    print(f"Address:      {entry.get('address')}")
    print(f"Symbol:       {entry.get('symbol')}")
    print(f"Status:       {entry.get('status')}")
    print(f"First seen:   {entry.get('first_seen_at')}")
    print(f"Last updated: {entry.get('last_updated_at')}")

    history = entry.get("history") or []
    print(f"\nHistory points: {len(history)}")
    if history:
        latest = history[-1]
        print("Latest data point:")
        print(f"  Score:              {latest.get('score')} (base={latest.get('base_score')}, momentum={latest.get('momentum_score')})")
        print(f"  Trend / Stage:      {latest.get('trend')} / {latest.get('stage')}")
        print(f"  Buy/sell pressure:  {latest.get('buy_sell_pressure')}")
        print(f"  Volume momentum:    {latest.get('volume_momentum')}")
        print(f"  Price acceleration: {latest.get('price_acceleration')}")
        print(f"  Persistence streak: {latest.get('persistence_streak')}")

    news = entry.get("news") or []
    print()
    if news:
        print(f"Active news signals ({len(news)}):")
        for signal in news:
            print(
                f"  [{signal.get('event_type')}] {signal.get('sentiment')} "
                f"(confidence={signal.get('confidence')}, bias={signal.get('directional_bias')}, "
                f"urgency={signal.get('urgency')}, id={signal.get('event_id')})"
            )
    else:
        print("No active news signals for this opportunity.")


# ---------------------------------------------------------------------------
# news
# ---------------------------------------------------------------------------

def _cmd_news(args):
    signals = signals_for_symbols([args.symbol]) if args.symbol else active_signals()

    if not signals:
        scope = f"mentioning {args.symbol}" if args.symbol else "right now"
        print(f"No active news signals {scope}.")
        return

    for signal in signals:
        assets = ", ".join(signal.get("affected_assets") or []) or "(none extracted)"
        print(
            f"[{signal.get('event_type')}] {signal.get('sentiment')} "
            f"(confidence={signal.get('confidence')}, bias={signal.get('directional_bias')}, "
            f"urgency={signal.get('urgency')})"
        )
        print(f"  assets={assets} source={signal.get('source')} id={signal.get('event_id')}")
    print(f"\n{len(signals)} active signal(s) shown.")


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------

_BREAKDOWN_LABELS = (
    ("by_score_bucket", "By score bucket"),
    ("by_trend", "By trend"),
    ("by_stage", "By stage"),
    ("by_entry_reason", "By entry reason"),
    ("by_volume_momentum_direction", "By volume momentum direction"),
    ("by_price_acceleration_direction", "By price acceleration direction"),
)


def _cmd_performance(args):
    report = analyze_recent(mode=args.mode, top_n_recent=args.top_n, since=args.since)

    if report.total_trades == 0:
        print(f"No closed trades found for mode={args.mode}.")
        return

    print(f"Trades analyzed: {report.total_trades} (wins={report.wins}, losses={report.losses}, breakeven={report.breakeven})")
    print(f"Win rate:         {_pct(report.win_rate)}")
    print(f"Loss rate:        {_pct(report.loss_rate)}")
    print(f"Average win:      {_usd(report.average_win_usd)}")
    print(f"Average loss:     {_usd(report.average_loss_usd)}")
    print(f"Profit factor:    {report.profit_factor if report.profit_factor is not None else 'N/A'}")
    print(f"Expectancy:       {_usd(report.expectancy_usd)}")
    print(f"Total PnL:        {_usd(report.total_pnl_usd)}")
    holding = report.average_holding_time_minutes
    print(f"Avg holding time: {f'{holding:.1f} min' if holding is not None else 'N/A'}")

    for attr, label in _BREAKDOWN_LABELS:
        breakdown = getattr(report, attr)
        if not breakdown:
            continue
        print(f"\n{label}:")
        for key, stats in breakdown.items():
            print(f"  {key}: trades={stats['trades']} win_rate={_pct(stats['win_rate'])} avg_pnl={_usd(stats['average_pnl_usd'])}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _cmd_status(args):
    print("=== Execution safety ===")
    print(f"EXECUTION_ENABLED_IN_CODE (src/wallet.py): {_read_execution_enabled_flag()}")
    print(f"LIVE_TRADING (.env):                       {_env_bool('LIVE_TRADING', False)}")

    print("\n=== Discovery / signal sources ===")
    pumpfun_state = "enabled" if pumpfun_is_configured() else "disabled (no PUMPFUN_API_KEY set)"
    print(f"Pump.fun discovery:          {pumpfun_state}")
    print(f"News -> watchlist link:      {'enabled' if NEWS_SIGNAL_WATCHLIST_LINK_ENABLED else 'disabled'}")
    print(f"Adaptive loop interval:      {'enabled' if _env_bool('RADAR_ADAPTIVE_INTERVAL_ENABLED', True) else 'disabled'}")

    print("\n=== Opportunity watchlist ===")
    all_entries = list_all()
    counts = {status: 0 for status in _STATUSES}
    for entry in all_entries:
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
    print(f"Total tracked: {len(all_entries)}")
    for status in _STATUSES:
        print(f"  {status}: {counts[status]}")

    print("\n=== News signal engine ===")
    print(f"Active signals: {len(active_signals())}")


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------

def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description=(
            "Read-only monitoring/analysis CLI for the radar, opportunity "
            "watchlist, news signal engine, and paper-trading performance. "
            "Never buys, sells, executes, or touches a wallet."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    watchlist_parser = subparsers.add_parser("watchlist", help="list tracked opportunities")
    watchlist_parser.add_argument(
        "--status", type=str.upper, choices=_STATUSES, default=None,
        help="filter by status (case-insensitive)",
    )

    opportunity_parser = subparsers.add_parser("opportunity", help="show full detail for one tracked address")
    opportunity_parser.add_argument("address", help="token address")

    news_parser = subparsers.add_parser("news", help="list active news signals")
    news_parser.add_argument("--symbol", default=None, help="filter by affected asset symbol")

    performance_parser = subparsers.add_parser("performance", help="print a trade performance report")
    performance_parser.add_argument("--mode", type=str.lower, choices=("paper", "live", "both"), default="paper")
    performance_parser.add_argument("--top-n", type=int, default=None, dest="top_n", help="only the N most recently closed trades")
    performance_parser.add_argument("--since", default=None, help="ISO date/time -- only trades closed at or after this")

    subparsers.add_parser("status", help="show system/source status")

    return parser


_COMMANDS = {
    "watchlist": _cmd_watchlist,
    "opportunity": _cmd_opportunity,
    "news": _cmd_news,
    "performance": _cmd_performance,
    "status": _cmd_status,
}


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    _COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
