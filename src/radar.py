"""Radar entry point.

Pipeline for each discovered Solana pair:

    fetch (dex_client)
        -> first-pass filter (liquidity / volume / buy-sell ratio)
        -> scoring.calculate_score        (0-100 base score)
        -> momentum.calculate_momentum    (0-75 momentum score)
        -> stage.classify_stage           (EARLY/RISING/MATURE/LATE by age)
        -> snapshot.save_snapshot         (persist for future observation)
        -> observation.analyze_observation (trend vs the previous snapshot)
        -> ranked, printed results

This module only discovers, scores and logs candidates. It does not place
any order and does not touch a wallet -- see README.md.
"""

import argparse
import logging
import time

from src.config import (
    MIN_BUY_SELL_RATIO,
    MIN_LIQUIDITY_USD,
    MIN_VOLUME_24H_USD,
    RADAR_LOOP_INTERVAL_SECONDS,
    RADAR_WATCHLIST_SIZE,
)
from src.dex_client import fetch_pairs, fetch_solana_token_addresses
from src.logging_config import setup_logging
from src.momentum import calculate_momentum
from src.observation import analyze_observation
from src.scoring import calculate_score
from src.snapshot import known_addresses, save_snapshot
from src.stage import classify_stage
from src.utils import safe_get

logger = logging.getLogger(__name__)


def _passes_first_filter(liquidity, volume, buys, sells):
    if liquidity is None or volume is None:
        return False

    return (
        liquidity >= MIN_LIQUIDITY_USD
        and volume >= MIN_VOLUME_24H_USD
        and buys >= MIN_BUY_SELL_RATIO * max(sells, 1)
    )


def _age_minutes(pair_created_at):
    if not isinstance(pair_created_at, (int, float)):
        return None
    return (time.time() * 1000 - pair_created_at) / 60000


def evaluate_pair(pair):
    """Run one pair through the full analysis pipeline and return a
    result dict, or None if the pair's data is too malformed to use.

    Never raises: any unexpected shape in a single pair is logged and
    skipped so it cannot take the whole radar run down with it.
    """
    if not isinstance(pair, dict):
        logger.warning("Skipping a malformed pair entry (not an object): %r", pair)
        return None

    try:
        base = pair.get("baseToken") or {}
        symbol = base.get("symbol", "?")
        address = base.get("address", "?")

        liquidity = safe_get(pair, "liquidity", "usd")
        volume = safe_get(pair, "volume", "h24")
        buys = safe_get(pair, "txns", "h24", "buys", default=0) or 0
        sells = safe_get(pair, "txns", "h24", "sells", default=0) or 0

        try:
            price_usd = float(pair.get("priceUsd")) if pair.get("priceUsd") is not None else None
        except (TypeError, ValueError):
            price_usd = None

        ok = _passes_first_filter(liquidity, volume, buys, sells)

        # Persist a snapshot for every pair we see (not just the ones that
        # pass the filter) so observation has history to compare against
        # once a token starts qualifying.
        save_snapshot(address, pair)

        base_score = calculate_score(pair)
        momentum_score = calculate_momentum(pair)
        final_score = round((base_score * 0.60) + (momentum_score * 0.40))

        age_minutes = _age_minutes(pair.get("pairCreatedAt"))
        stage = classify_stage(age_minutes)

        observation = analyze_observation(address)

        return {
            "score": final_score,
            "base_score": base_score,
            "momentum_score": momentum_score,
            "ok": ok,
            "symbol": symbol,
            "stage": stage,
            "age": age_minutes,
            "liquidity": liquidity,
            "volume": volume,
            "buys": buys,
            "sells": sells,
            "address": address,
            "price_usd": price_usd,
            "trend": observation.get("status") if observation.get("status") != "OK" else observation.get("trend"),
        }
    except Exception:
        # Defensive backstop: one bad pair should never abort the run.
        logger.exception("Unexpected error while evaluating a pair (symbol lookup failed too) -- skipping it")
        return None


def run_radar():
    """Fetch, score and rank current Solana candidates. Returns the list
    of result dicts (possibly empty) -- never raises for network/data
    problems, since dex_client and evaluate_pair already degrade
    gracefully and log what happened.

    Queries both newly-discovered addresses (DexScreener's "latest
    profiles" feed) and a watchlist of previously-seen addresses
    (RADAR_WATCHLIST_SIZE, from data/snapshots.json), so a token keeps
    accumulating snapshots across cycles instead of only ever getting
    one -- that's what lets observation.py report a real trend instead
    of INSUFFICIENT_DATA once the radar has run more than once.
    """
    discovered = fetch_solana_token_addresses()
    watchlist = known_addresses(limit=RADAR_WATCHLIST_SIZE) if RADAR_WATCHLIST_SIZE > 0 else []

    # Preserve order (newest discoveries first) while de-duplicating.
    addresses = list(dict.fromkeys(discovered + watchlist))

    if watchlist:
        logger.info(
            "Querying %s address(es): %s newly discovered + %s from the watchlist",
            len(addresses), len(discovered), len(watchlist),
        )

    if not addresses:
        logger.warning("No Solana token addresses discovered this run -- nothing to score")
        return []

    pairs = fetch_pairs(addresses)

    if not pairs:
        logger.warning("No market pairs returned for the discovered addresses")
        return []

    results = []
    for pair in pairs:
        result = evaluate_pair(pair)
        if result is not None:
            results.append(result)

    results.sort(key=lambda item: item["score"], reverse=True)
    return results


def _format_line(item):
    age_text = f"{item['age']:.1f}m" if item["age"] is not None else "N/A"
    status = "PASS" if item["ok"] else "REJECT"
    liq = "N/A" if item["liquidity"] is None else f"${item['liquidity']:,.0f}"
    vol = "N/A" if item["volume"] is None else f"${item['volume']:,.0f}"

    return (
        f"[{status}] {item['symbol']:<10} | FINAL={item['score']:>3}/100 "
        f"(base={item['base_score']}, momentum={item['momentum_score']}) | "
        f"{item['stage']:<8} | age={age_text:>8} | liq={liq:>12} | "
        f"vol24h={vol:>12} | buys={item['buys']} sells={item['sells']} | "
        f"trend={item['trend']}"
    )


def run_once(on_results=None):
    """One discover-score-print cycle. Returns the results list.
    on_results(results), if given, is called after printing -- this is
    the hook src.paper_trader (or any future consumer) plugs into,
    keeping radar.py itself unaware of what trading logic exists.
    Exceptions from on_results are logged, not raised, so a problem in a
    downstream consumer never breaks the radar's own loop.
    """
    results = run_radar()
    passed = sum(1 for item in results if item["ok"])

    print()
    print("=== FINAL RANKED RESULTS ===")
    for item in results:
        print(_format_line(item))

    print()
    print(f"Pairs evaluated: {len(results)} | Pairs passing first filter: {passed}")
    logger.info("Radar run complete: %s evaluated, %s passed the first filter", len(results), passed)

    if on_results is not None:
        try:
            on_results(results)
        except Exception:
            logger.exception("on_results callback failed -- continuing the radar loop regardless")

    return results


def run_forever(interval_seconds=None, max_iterations=None, on_results=None):
    """Run run_once() repeatedly, sleeping interval_seconds between
    cycles, until interrupted (Ctrl+C) or max_iterations is reached
    (None = forever -- max_iterations exists mainly for bounded demo/test
    runs). One cycle raising does not stop the loop: it is logged and
    the loop continues after the usual sleep.
    """
    interval_seconds = RADAR_LOOP_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    logger.info("Starting continuous radar loop (interval=%ss)", interval_seconds)

    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            logger.info("--- Radar cycle %s ---", iteration)
            try:
                run_once(on_results=on_results)
            except Exception:
                logger.exception("Radar cycle %s failed -- will retry next cycle", iteration)

            if max_iterations is not None and iteration >= max_iterations:
                break

            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("Radar loop stopped by user (Ctrl+C) after %s cycle(s)", iteration)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Solana meme-token discovery radar")
    parser.add_argument("--loop", action="store_true", help="run continuously instead of once")
    parser.add_argument("--interval", type=float, default=None, help="seconds between cycles in --loop mode")
    parser.add_argument("--max-iterations", type=int, default=None, help="stop --loop after N cycles (mainly for testing)")
    parser.add_argument("--paper", action="store_true", help="run paper trading decisions on each cycle's results")
    return parser.parse_args(argv)


def main(argv=None):
    setup_logging()
    args = _parse_args(argv)

    on_results = None
    if args.paper:
        from src.paper_trader import run_paper_cycle  # imported lazily so --paper stays opt-in

        on_results = run_paper_cycle
        logger.info("Paper trading is ENABLED for this run -- simulated only, no real funds or orders")

    if args.loop:
        run_forever(interval_seconds=args.interval, max_iterations=args.max_iterations, on_results=on_results)
    else:
        logger.info("Starting radar run")
        run_once(on_results=on_results)


if __name__ == "__main__":
    main()
