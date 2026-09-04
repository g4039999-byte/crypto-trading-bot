"""Live-trading decision AND real-execution orchestration layer for US
stocks. Mirrors src/live_trader.py (crypto side) closely, INCLUDING its
single most important property: this module is NEVER imported by
src.stocks.engine (the continuously-running paper-trading loop) or by
webapp/app.py's process control -- exactly the precedent the crypto side
already set (src/radar.py and the running webapp process never import
src/live_trader.py either; see src/cli.py's isolation docstring). The
capability to run a live check exists here, fully implemented and unit-
tested against mocks only, but nothing in the actually-running system
ever calls it. It can only be invoked by a human, deliberately, from a
separate script/shell/REPL -- and even then, every path below still
requires ALL THREE gate layers open (src.stocks.kill_switch.
trading_allowed() for Layers 2+3, src.stocks.live_broker.
STOCKS_EXECUTION_ENABLED_IN_CODE for Layer 1), re-checked fresh on every
single decision. See STOCKS_LIVE_TRADING_GATE.md for the full runbook
before ever using this module for real.

Exit logic reuses src.stocks.risk_engine's check_exit()/
update_trailing_stop()/stop_loss_price()/take_profit_price() completely
unchanged -- the exact same, already-deeply-tested ATR math paper
trading and the backtester use, so a live position's stop/target/
trailing behavior is provably identical to what was validated in
STOCKS_LIVE_READINESS_REPORT.md, not a second reimplementation that
could quietly drift from it.
"""

import logging
import uuid
from datetime import datetime, timezone

from src.stocks import kill_switch, live_broker, live_ledger, live_risk
from src.stocks.config import STOCKS_LIVE_MIN_BUYING_POWER_BUFFER_USD, STOCKS_LIVE_STARTING_CAPITAL_USD
from src.stocks.live_logger import log_decision
from src.stocks.risk_engine import check_exit, update_trailing_stop

logger = logging.getLogger(__name__)


def _new_client_order_id(kind, symbol):
    return f"stocks-live-{kind}-{symbol}-{uuid.uuid4().hex[:12]}"


def _held_days(position):
    opened_at = datetime.fromisoformat(position["opened_at"])
    return (datetime.now(timezone.utc) - opened_at).total_seconds() / 86400


def evaluate_live_entry(symbol, price, atr_value, *, strategy=None, score=None, regime=None):
    """Pure decision layer: checks the three-layer trading gate,
    duplicate-position/duplicate-order protection (both the local
    ledger AND a fresh read of Alpaca's own open orders), live risk
    gating (position count, daily loss, circuit breaker, overtrading
    guard), and real buying power -- WITHOUT placing an order. Returns
    {"action": "BLOCKED"|"SKIP"|"BUY", "reason": str, ...}. Every branch
    is logged via live_logger, including every refusal, not just BUYs.
    """
    gate = kill_switch.trading_allowed()
    if not gate.allowed:
        reason = "; ".join(gate.reasons)
        log_decision("BLOCKED", symbol, reason)
        return {"action": "BLOCKED", "reason": reason}

    if live_ledger.has_open_position(symbol):
        reason = "a live position is already open for this symbol -- not opening a second one"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    # Duplicate-order protection, layer 2: a fresh check against Alpaca
    # itself (not just the local ledger, which could theoretically be
    # stale/lost) for any already-working order on this symbol.
    open_orders = live_broker.list_live_open_orders(symbol)
    if open_orders:
        reason = f"{len(open_orders)} open real order(s) already exist for this symbol at Alpaca -- not submitting another"
        log_decision("BLOCKED", symbol, reason)
        return {"action": "BLOCKED", "reason": reason}

    state = live_ledger.load_state()
    room_ok, room_reason = live_risk.can_open_new_live_position(state)
    if not room_ok:
        log_decision("SKIP", symbol, room_reason)
        return {"action": "SKIP", "reason": room_reason}

    if not price or price <= 0 or not atr_value or atr_value <= 0:
        reason = "no usable price/ATR -- cannot size a volatility-based stop/target"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    account = live_broker.get_live_account()
    if not account:
        reason = "could not read live account balance/buying power -- refusing to size a real order blind"
        log_decision("BLOCKED", symbol, reason)
        return {"action": "BLOCKED", "reason": reason}

    try:
        buying_power = float(account.get("buying_power", 0))
    except (TypeError, ValueError):
        buying_power = 0.0
    usable_buying_power = buying_power - STOCKS_LIVE_MIN_BUYING_POWER_BUFFER_USD
    if usable_buying_power <= 0:
        reason = f"insufficient live buying power (${buying_power:.2f} reported, need a ${STOCKS_LIVE_MIN_BUYING_POWER_BUFFER_USD:.2f} safety buffer)"
        log_decision("SKIP", symbol, reason, extra={"buying_power": buying_power})
        return {"action": "SKIP", "reason": reason}

    regime_mult = 1.0
    if regime:
        try:
            from src.stocks.regime import risk_multiplier
            regime_mult = risk_multiplier(regime)
        except Exception:
            logger.exception("risk_multiplier lookup failed for live sizing -- using 1.0")

    size_usd = live_risk.compute_live_position_size_usd(state, regime_mult, buying_power_usd=usable_buying_power)
    if size_usd <= 0:
        reason = "no capital room left under the live deployment cap / available buying power"
        log_decision("SKIP", symbol, reason)
        return {"action": "SKIP", "reason": reason}

    reason = f"passed all live gates and risk checks: score={score} strategy={strategy} size_usd={size_usd}"
    log_decision("BUY", symbol, reason, extra={"score": score, "strategy": strategy, "size_usd": size_usd, "decision_only": True})
    return {"action": "BUY", "reason": reason, "size_usd": size_usd, "atr": atr_value, "strategy": strategy, "score": score}


def attempt_live_buy(symbol, price, decision):
    """Actually submit and confirm a real BUY. Only ever reached once
    evaluate_live_entry() has already returned "BUY" -- meaning every
    gate/risk/balance check above already passed. Still refuses at
    live_broker's own Layer-1 check regardless. Never raises: every
    failure mode is caught and logged; local bookkeeping
    (live_ledger.record_open_position) is only ever updated after a real
    fill is confirmed. Returns {"executed": bool, "reason": str, ...}.
    """
    size_usd = decision["size_usd"]
    shares = round(size_usd / price, 4) if price else 0
    if shares <= 0:
        reason = "computed share quantity is zero or negative -- not submitting"
        log_decision("BLOCKED", symbol, reason)
        return {"executed": False, "reason": reason}

    client_order_id = _new_client_order_id("buy", symbol)
    try:
        order = live_broker.submit_live_order(symbol, shares, "buy", client_order_id=client_order_id)
    except live_broker.LiveTradingDisabled as exc:
        # Expected in every configuration this project has ever run with.
        reason = f"real execution is disabled: {exc}"
        log_decision("BLOCKED", symbol, reason)
        return {"executed": False, "reason": reason}
    except live_broker.LiveNotConfigured as exc:
        reason = f"live account not configured: {exc}"
        log_decision("BLOCKED", symbol, reason)
        return {"executed": False, "reason": reason}
    except live_broker.LiveOrderRejected as exc:
        reason = f"order rejected by Alpaca: {exc}"
        logger.error("Live BUY for %s rejected: %s", symbol, exc)
        log_decision("ERROR", symbol, reason, extra={"client_order_id": client_order_id})
        return {"executed": False, "reason": reason}
    except live_broker.LiveOrderAmbiguous as exc:
        reason = f"order submission outcome is ambiguous -- NOT retried, needs manual reconciliation: {exc}"
        logger.error("Live BUY for %s ambiguous: %s", symbol, exc)
        log_decision("UNCONFIRMED", symbol, reason, extra={"client_order_id": client_order_id})
        return {"executed": False, "reason": reason, "ambiguous": True, "client_order_id": client_order_id}

    fill = live_broker.poll_order_fill(order["id"])
    if not fill["filled"]:
        reason = f"order not filled (status={fill.get('status')}, timed_out={fill.get('timed_out')})"
        action = "UNCONFIRMED" if fill.get("timed_out") else "SKIP"
        log_decision(action, symbol, reason, extra={"order_id": order.get("id"), "client_order_id": client_order_id})
        return {"executed": False, "reason": reason, "order_id": order.get("id")}

    try:
        fill_price = float(fill["filled_avg_price"]) if fill.get("filled_avg_price") else price
    except (TypeError, ValueError):
        fill_price = price
    try:
        fill_qty = float(fill["filled_qty"]) if fill.get("filled_qty") else shares
    except (TypeError, ValueError):
        fill_qty = shares

    position = live_ledger.record_open_position(
        symbol, fill_price, fill_qty, round(fill_price * fill_qty, 2), decision["atr"],
        order_id=order.get("id"), client_order_id=client_order_id,
        strategy=decision.get("strategy"), entry_score=decision.get("score"), entry_reason=decision.get("reason"),
        starting_capital_usd=STOCKS_LIVE_STARTING_CAPITAL_USD,
    )
    log_decision("BUY", symbol, "real order filled", extra={"order_id": order.get("id"), "client_order_id": client_order_id, "fill_price": fill_price, "fill_qty": fill_qty})
    return {"executed": True, "position": position, "order_id": order.get("id")}


def evaluate_live_exit(position, current_price):
    """Pure decision over the exact same risk_engine.check_exit() logic
    paper trading and the backtester use. Also refreshes the position's
    trailing-stop level first (update_trailing_stop), same as
    src.stocks.paper_broker.evaluate_exit_for_open_positions() does --
    returns (decision_dict, possibly_updated_trailing_stop_price).
    """
    new_trailing = update_trailing_stop(position, current_price)
    position_for_check = {**position, "trailing_stop_price": new_trailing} if new_trailing != position.get("trailing_stop_price") else position
    should_exit, reason = check_exit(position_for_check, current_price, _held_days(position))
    action = "SELL" if should_exit else "HOLD"
    return {"action": action, "reason": reason}, new_trailing


def attempt_live_sell(position, current_price, reason):
    """Actually submit and confirm a real SELL of an existing live
    position. Re-checks the trading gate itself (an exit should still be
    allowed to happen even if new entries are blocked in most designs --
    but for stocks this project deliberately treats an exit exactly like
    an entry: if the gate is closed, no order of any kind is placed for
    real; a closed gate has, in every run this project has ever done,
    meant no live position exists to exit in the first place). Never
    raises -- see attempt_live_buy()'s docstring for the failure-mode
    shape this mirrors.
    """
    symbol = position["symbol"]

    gate = kill_switch.trading_allowed()
    if not gate.allowed:
        blocked_reason = "; ".join(gate.reasons)
        log_decision("BLOCKED", symbol, blocked_reason)
        return {"executed": False, "reason": blocked_reason}

    shares = position["shares"]
    client_order_id = _new_client_order_id("sell", symbol)
    try:
        order = live_broker.submit_live_order(symbol, shares, "sell", client_order_id=client_order_id)
    except live_broker.LiveTradingDisabled as exc:
        reason2 = f"real execution is disabled: {exc}"
        log_decision("BLOCKED", symbol, reason2)
        return {"executed": False, "reason": reason2}
    except live_broker.LiveNotConfigured as exc:
        reason2 = f"live account not configured: {exc}"
        log_decision("BLOCKED", symbol, reason2)
        return {"executed": False, "reason": reason2}
    except live_broker.LiveOrderRejected as exc:
        reason2 = f"order rejected by Alpaca: {exc}"
        logger.error("Live SELL for %s rejected: %s", symbol, exc)
        log_decision("ERROR", symbol, reason2, extra={"client_order_id": client_order_id})
        return {"executed": False, "reason": reason2}
    except live_broker.LiveOrderAmbiguous as exc:
        reason2 = f"order submission outcome is ambiguous -- NOT retried, needs manual reconciliation: {exc}"
        logger.error("Live SELL for %s ambiguous: %s", symbol, exc)
        log_decision("UNCONFIRMED", symbol, reason2, extra={"client_order_id": client_order_id})
        return {"executed": False, "reason": reason2, "ambiguous": True, "client_order_id": client_order_id}

    fill = live_broker.poll_order_fill(order["id"])
    if not fill["filled"]:
        reason2 = f"order not filled (status={fill.get('status')}, timed_out={fill.get('timed_out')})"
        action = "UNCONFIRMED" if fill.get("timed_out") else "SKIP"
        log_decision(action, symbol, reason2, extra={"order_id": order.get("id"), "client_order_id": client_order_id})
        return {"executed": False, "reason": reason2, "order_id": order.get("id")}

    try:
        fill_price = float(fill["filled_avg_price"]) if fill.get("filled_avg_price") else current_price
    except (TypeError, ValueError):
        fill_price = current_price

    result = live_ledger.record_close_position(
        symbol, fill_price, reason, order_id=order.get("id"), client_order_id=client_order_id,
        starting_capital_usd=STOCKS_LIVE_STARTING_CAPITAL_USD,
    )
    log_decision("SELL", symbol, "real order filled", extra={"order_id": order.get("id"), "client_order_id": client_order_id, "fill_price": fill_price, "exit_reason": reason})
    return {"executed": True, "result": result, "order_id": order.get("id")}


def emergency_stop(reason="manual emergency stop"):
    """Immediate kill switch (takes effect on the very next decision, no
    restart needed) PLUS a best-effort cancel of every open real order.
    Safe to call at any time, including the normal state where every
    gate is already closed and there is nothing real to cancel. This is
    the single function a human would call to halt everything
    immediately if stocks live trading were ever active.
    """
    kill_switch.engage_kill_switch(reason)
    try:
        live_broker.cancel_all_live_orders()
    except (live_broker.LiveTradingDisabled, live_broker.LiveNotConfigured):
        logger.info("Emergency stop: no real orders could exist yet (live execution gate/config not open) -- kill switch alone is sufficient")
    except Exception:
        logger.exception("Emergency cancel-all-orders raised unexpectedly -- the kill switch is still engaged regardless")
