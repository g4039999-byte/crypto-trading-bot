"""Orchestrates one full stocks paper-trading cycle: discovery -> regime
-> scoring (market data + strategies + optional X social) -> risk-gated
paper entry -> monitoring existing positions -> exit. Mirrors src/
radar.py + src/paper_trader.py's combined role on the crypto side, as
one module since the two sides are small enough here not to need the
crypto side's separate-file split.

Entry point: run_cycle() (one pass) / run_forever() (continuous, same
shape as src.radar.run_forever). `python -m src.stocks.run --loop`.

Resilience: a data-provider outage, Alpaca being unconfigured/down, or
X being unavailable/unconfigured can never crash a cycle -- each stage
degrades to "no candidates"/"no signal" and logs what happened, exactly
the crypto side's convention. See tests/stocks/test_engine.py's
resilience tests.
"""

import logging
import time

from src.stocks import health
from src.stocks.config import STOCKS_LOOP_INTERVAL_SECONDS, STOCKS_MARKET_CLOSED_POLL_SECONDS, STOCKS_MIN_SCORE, STOCKS_RESPECT_MARKET_HOURS
from src.stocks.data_provider import get_provider
from src.stocks.discovery import scan_universe
from src.stocks.market_hours import is_market_open, seconds_until_next_open
from src.stocks.paper_broker import evaluate_exit_for_open_positions, load_state, open_position
from src.stocks.paper_logger import log_decision
from src.stocks.regime import current_regime, risk_multiplier
from src.stocks.risk_engine import can_open_new_position, compute_position_size_usd
from src.stocks.scoring import calculate_score

logger = logging.getLogger(__name__)


def _x_social_signal(symbol, features):
    """Best-effort X social signal for this symbol, reusing src.
    x_intelligence unchanged (it was already generic/symbol-based).
    Stocks have no on-chain "address" -- the symbol doubles as its own
    unique key, which src.x_correlation.correlate() only ever uses as
    an opaque dict key, so this needs no change to that module. Never
    raises: any failure here yields "no signal", same resilience
    guarantee the crypto side already has.
    """
    try:
        from src import x_intelligence
        candidate = {"symbol": symbol, "address": symbol, "liquidity": features.get("volume"), "age": None}
        signal = x_intelligence.social_signal_for_token(symbol, [candidate])
        bonus = x_intelligence.score_bonus_for_signal(signal)
        return signal, bonus
    except Exception:
        logger.exception("X social signal lookup failed for %s -- continuing with market-only score", symbol)
        return None, 0


def evaluate_entry(symbol, candidate, regime, state):
    """candidate: {"features": {...}, "df": DataFrame} from
    discovery.scan_universe(). Returns a decision dict, same shape as
    the crypto side's evaluate_entry: {"action": "BUY"|"SKIP",
    "reason": str, "size_usd": float | None, ...scoring context...}.
    """
    features, df = candidate["features"], candidate["df"]

    already_held = any(p["symbol"] == symbol for p in state.get("open_positions", []))
    if already_held:
        reason = "already holding an open position in this symbol"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    room_ok, room_reason = can_open_new_position(state, regime)
    if not room_ok:
        log_decision("SKIP", symbol, room_reason)
        return {"action": "SKIP", "reason": room_reason}

    x_signal, social_bonus = _x_social_signal(symbol, features)
    try:
        from src.stocks.strategy_registry import get_active_strategy
        active_strategy = get_active_strategy()
    except Exception:
        active_strategy = None
    scored = calculate_score(features, df, regime, social_bonus=social_bonus, active_strategy=active_strategy)

    if scored["score"] < STOCKS_MIN_SCORE:
        reason = f"score {scored['score']} below minimum {STOCKS_MIN_SCORE}"
        log_decision("SKIP", symbol, reason, extra={"score": scored["score"]})
        return {"action": "SKIP", "reason": reason, "score": scored["score"]}

    if not scored["best_strategy"]:
        reason = "no strategy produced a BUY signal for this candidate"
        log_decision("SKIP", symbol, reason, extra={"score": scored["score"]})
        return {"action": "SKIP", "reason": reason, "score": scored["score"]}

    size_usd = compute_position_size_usd(state, risk_multiplier(regime))
    if size_usd <= 0:
        reason = "no capital room left under the deployment cap"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    atr_value = features.get("atr")
    if not atr_value or atr_value <= 0:
        reason = "no usable ATR -- cannot size a volatility-based stop/target"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    reason = (
        f"score {scored['score']}>={STOCKS_MIN_SCORE}, strategy={scored['best_strategy']} "
        f"({scored['strategy_reason']}), regime={regime.get('trend')}/{regime.get('risk_appetite')}"
    )
    if x_signal:
        reason += f", X signal: {x_signal['entity']} (confidence {x_signal['confidence']:.2f})"

    log_decision("BUY", symbol, reason, extra={"score": scored["score"], "strategy": scored["best_strategy"], "size_usd": size_usd})

    return {
        "action": "BUY", "reason": reason, "size_usd": size_usd,
        "score": scored["score"], "strategy": scored["best_strategy"],
        "atr": atr_value, "features": features, "regime": regime,
        "x_entity": x_signal["entity"] if x_signal else None,
    }


def run_cycle():
    """One full cycle. Returns a summary dict (counts, for the funnel
    log / dashboard) -- never raises: every stage is wrapped so a
    failure anywhere degrades this cycle to "did less", not "crashed".
    """
    summary = {"scanned": 0, "candidates": 0, "buys": 0, "sells": 0, "skips": 0}

    try:
        regime = current_regime()
    except Exception:
        logger.exception("Regime detection failed -- using a conservative default")
        regime = {"trend": "SIDEWAYS", "risk_appetite": "risk-off", "volatility": "HIGH"}

    try:
        candidates = scan_universe()
    except Exception:
        logger.exception("Universe scan failed entirely this cycle")
        candidates = {}
    summary["candidates"] = len(candidates)

    # Monitor + exit existing positions first (uses live prices for
    # every open position, not just this cycle's scanned candidates --
    # a position can be open on a symbol that later drops out of the
    # scan's first-pass filter, and must still be monitored).
    try:
        state = load_state()
        open_symbols = [p["symbol"] for p in state.get("open_positions", [])]
        provider = get_provider()
        current_prices = {}
        for symbol in open_symbols:
            price = candidates.get(symbol, {}).get("features", {}).get("price")
            if price is None:
                price = provider.get_latest_price(symbol)
            if price is not None:
                current_prices[symbol] = price
        closed = evaluate_exit_for_open_positions(current_prices)
        summary["sells"] = len(closed)
    except Exception:
        logger.exception("Exit monitoring failed this cycle")

    state = load_state()
    ranked = sorted(candidates.items(), key=lambda kv: kv[1]["features"].get("relative_volume") or 0, reverse=True)
    for symbol, candidate in ranked:
        summary["scanned"] += 1
        decision = evaluate_entry(symbol, candidate, regime, state)
        if decision["action"] == "BUY":
            open_position(
                symbol, candidate["features"]["price"], decision["size_usd"], decision["atr"],
                strategy=decision["strategy"], entry_score=decision["score"], entry_reason=decision["reason"],
                features_snapshot=candidate["features"], regime_snapshot=regime, x_entity=decision.get("x_entity"),
            )
            summary["buys"] += 1
            state = load_state()  # refresh so the next candidate's room check sees this one
        else:
            summary["skips"] += 1

    summary["regime"] = regime
    logger.info(
        "Stocks cycle: %s candidate(s) passed filter -> %s BUY, %s SELL, %s SKIP (regime=%s/%s)",
        summary["candidates"], summary["buys"], summary["sells"], summary["skips"],
        regime.get("trend"), regime.get("risk_appetite"),
    )
    _write_last_cycle_status(summary)
    return summary


def _write_last_cycle_status(summary):
    """Persist a small, cheap-to-read snapshot of the last cycle for
    the dashboard (webapp/app.py) -- so a status poll every few seconds
    never itself triggers a live data-provider/regime fetch; it just
    reads this file. Best-effort: a write failure here never affects
    the cycle that just ran.
    """
    try:
        import json
        from datetime import datetime, timezone
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent.parent / "data" / "stocks" / "last_cycle.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**summary, "completed_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Could not persist last-cycle status -- non-fatal")


def _maybe_wait_for_market_open():
    """If STOCKS_RESPECT_MARKET_HOURS is on and the market is closed
    right now, sleep in short, interruptible polls (never one giant
    sleep) until either it opens or STOCKS_MARKET_CLOSED_POLL_SECONDS
    worth of waiting has happened -- returns without blocking further
    either way, so the caller's own loop stays responsive (KeyboardInterrupt,
    a future stop signal) instead of one multi-hour time.sleep() call.
    Returns True if the market is open (or gating is disabled) and a
    real cycle should run this iteration, False if it should skip.
    """
    if not STOCKS_RESPECT_MARKET_HOURS:
        return True
    if is_market_open():
        return True
    wait = min(seconds_until_next_open(), STOCKS_MARKET_CLOSED_POLL_SECONDS)
    logger.info("US market closed -- sleeping %.0fs before checking again", wait)
    time.sleep(max(1.0, wait))
    return False


def _maybe_run_learning_cycle():
    """Best-effort periodic strategy re-evaluation -- see
    src.stocks.learning_engine's module docstring for the full pipeline
    (analyze -> backtest candidates -> walk-forward compare -> adopt
    only a real, significant improvement -> or roll back a degrading
    active strategy). Never lets a learning-cycle failure take down the
    trading loop itself.
    """
    try:
        from src.stocks.learning_engine import maybe_run_learning_cycle
        maybe_run_learning_cycle()
    except Exception:
        logger.exception("Learning cycle failed this iteration -- trading loop continues unaffected")


def run_forever(interval_seconds=None, max_iterations=None):
    """Continuous loop with three layers of resilience on top of a
    plain run_cycle():
    1. Market-hours gating (_maybe_wait_for_market_open) -- skip real
       cycles while the exchange is closed instead of polling data
       providers for nothing.
    2. Health-tracked exponential backoff (src.stocks.health) -- a
       cycle that raises (rate limit, timeout, connection error, any
       transient data-provider failure) is retried automatically with
       a growing, capped delay, never treated as a reason to stop; the
       failure streak, outage start time, and recovery are all recorded
       for the dashboard's System/Data-Source Health.
    3. A periodic, best-effort self-learning check (_maybe_run_learning_cycle).

    A single unhandled KeyboardInterrupt (Ctrl+C) is still the only
    thing that stops this loop early -- everything else is retried.
    """
    interval_seconds = STOCKS_LOOP_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    logger.info("Starting continuous stocks paper-trading loop (interval=%ss)", interval_seconds)

    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            if not _maybe_wait_for_market_open():
                continue  # market still closed -- don't count this as a cycle or run one

            iteration += 1
            logger.info("--- Stocks cycle %s ---", iteration)
            try:
                summary = run_cycle()
                health.record_success(summary={k: v for k, v in summary.items() if k != "regime"})
                sleep_for = interval_seconds
            except Exception as exc:
                logger.exception("Stocks cycle %s failed -- auto-recovering", iteration)
                sleep_for = health.record_failure(repr(exc))

            _maybe_run_learning_cycle()

            if max_iterations is not None and iteration >= max_iterations:
                break
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("Stocks loop stopped by user (Ctrl+C) after %s cycle(s)", iteration)
