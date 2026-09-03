"""Historical backtest engine for the daily-bar strategies (momentum,
breakout, mean_reversion, pullback, relative_volume -- vwap_reclaim is
intraday-only, see its own module docstring for why it isn't covered
here). Uses
src.stocks.data_provider (yfinance by default -- free, no account
needed) so this runs with zero configuration.

Look-ahead discipline: a signal on day i is computed from bars[:i+1]
only (never a later bar), and if it fires, the fill price is day i+1's
OPEN, not day i's close -- the earliest price actually achievable by a
decision made after day i's bar closed. Exits are checked against each
subsequent day's own high/low (stop before target if both would have
triggered the same day -- the conservative assumption when daily bars
can't say which came first intraday).

Walk-forward: BACKTEST_IN_SAMPLE_FRACTION splits each symbol's date
range into an earlier in-sample portion and a later out-of-sample
portion, and results are reported for both separately -- since these
strategies use fixed, hand-set thresholds rather than parameters fit to
this data, this isn't "tune then validate" so much as "does this
strategy's edge hold up in a later, unseen period or was the in-sample
result a fluke of that specific window". A strategy whose out-of-sample
numbers collapse relative to in-sample is a straightforward overfitting/
regime-specific warning sign even without any parameter fitting having
happened.
"""

import logging
from dataclasses import dataclass, field

from src.stocks.config import BACKTEST_IN_SAMPLE_FRACTION, STOCKS_MAX_HOLDING_DAYS
from src.stocks.data_provider import get_provider
from src.stocks.features import compute_features
from src.stocks.risk_engine import stop_loss_price, take_profit_price
from src.stocks.strategies import STRATEGIES

logger = logging.getLogger(__name__)

MIN_BARS_BEFORE_TRADING = 55  # enough history for SMA50/ATR14 to be meaningful


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    entry_date: str
    entry_price: float
    exit_date: str = None
    exit_price: float = None
    reason: str = None
    pnl_pct: float = None
    confidence: float = None
    in_sample: bool = True


def _backtest_one_symbol(strategy_name, symbol, df, split_index):
    strategy_module = STRATEGIES[strategy_name]
    trades = []
    position = None

    for i in range(MIN_BARS_BEFORE_TRADING, len(df)):
        if position is not None:
            day = df.iloc[i]
            held_days = i - position["entry_index"]
            exit_price, reason = None, None
            if day["low"] <= position["stop_loss_price"]:
                exit_price, reason = position["stop_loss_price"], "stop_loss"
            elif day["high"] >= position["take_profit_price"]:
                exit_price, reason = position["take_profit_price"], "take_profit"
            elif held_days >= STOCKS_MAX_HOLDING_DAYS:
                exit_price, reason = float(day["close"]), "max_holding_time"

            if exit_price is not None:
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                trades.append(BacktestTrade(
                    symbol=symbol, strategy=strategy_name,
                    entry_date=str(df.index[position["entry_index"]]), entry_price=position["entry_price"],
                    exit_date=str(df.index[i]), exit_price=float(exit_price), reason=reason,
                    pnl_pct=float(pnl_pct), confidence=position["confidence"],
                    in_sample=position["entry_index"] < split_index,
                ))
                position = None
            continue  # never also evaluate a fresh entry the same day a position was open

        if i + 1 >= len(df):
            break  # no next-day open left to fill a fresh entry at

        window = df.iloc[: i + 1]
        try:
            features = compute_features(window)
            signal = strategy_module.generate_signal(features, window)
        except Exception:
            continue

        if signal["action"] != "BUY":
            continue

        atr_value = features.get("atr")
        if not atr_value or atr_value <= 0:
            continue

        entry_price = float(df["open"].iloc[i + 1])
        position = {
            "entry_index": i + 1, "entry_price": entry_price,
            "stop_loss_price": stop_loss_price(entry_price, atr_value),
            "take_profit_price": take_profit_price(entry_price, atr_value),
            "confidence": signal["confidence"],
        }

    return trades


def backtest_strategy(strategy_name, symbols, lookback_days=730):
    """Returns the list of resolved BacktestTrade objects across every
    symbol (a position still open at the end of a symbol's data is
    dropped, not counted -- same "unresolved, exclude from stats"
    convention scripts/backtest_paper_strategy.py uses on the crypto
    side). Never raises: a symbol with unusable data is logged and
    skipped.
    """
    if strategy_name not in STRATEGIES:
        raise KeyError(f"Unknown strategy {strategy_name!r}")
    if getattr(STRATEGIES[strategy_name], "TIMEFRAME", "daily") != "daily":
        raise ValueError(
            f"{strategy_name} is not backtestable on daily bars (TIMEFRAME="
            f"{getattr(STRATEGIES[strategy_name], 'TIMEFRAME', None)!r}) -- see its module docstring"
        )

    provider = get_provider()
    bars_by_symbol = provider.get_daily_bars_batch(list(symbols), lookback_days)

    all_trades = []
    for symbol, df in bars_by_symbol.items():
        if df is None or df.empty or len(df) < MIN_BARS_BEFORE_TRADING + 2:
            continue
        split_index = int(len(df) * BACKTEST_IN_SAMPLE_FRACTION)
        try:
            all_trades.extend(_backtest_one_symbol(strategy_name, symbol, df, split_index))
        except Exception:
            logger.exception("Backtest failed for %s on %s -- skipping this symbol", strategy_name, symbol)

    return all_trades


def backtest_all_strategies(symbols, lookback_days=730):
    """{strategy_name: [BacktestTrade, ...]} for every registered
    daily-bar strategy -- one batched data fetch shared across all of
    them (the expensive part), not one per strategy.
    """
    provider = get_provider()
    bars_by_symbol = provider.get_daily_bars_batch(list(symbols), lookback_days)

    results = {}
    for strategy_name, module in STRATEGIES.items():
        if getattr(module, "TIMEFRAME", "daily") != "daily":
            continue  # e.g. vwap_reclaim -- intraday-only, see its module docstring
        trades = []
        for symbol, df in bars_by_symbol.items():
            if df is None or df.empty or len(df) < MIN_BARS_BEFORE_TRADING + 2:
                continue
            split_index = int(len(df) * BACKTEST_IN_SAMPLE_FRACTION)
            try:
                trades.extend(_backtest_one_symbol(strategy_name, symbol, df, split_index))
            except Exception:
                logger.exception("Backtest failed for %s on %s -- skipping this symbol", strategy_name, symbol)
        results[strategy_name] = trades
    return results
