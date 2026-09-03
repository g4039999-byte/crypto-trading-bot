"""Configuration for the US stocks paper-trading subsystem. Entirely
separate from src/config.py (the crypto side) -- no shared state, no
shared env var names, on purpose.

All values overridable via environment variables / a local .env file
(never committed -- see .env.example), same convention as the crypto
side. No secrets live in this file; ALPACA_API_KEY/ALPACA_API_SECRET
are read from the environment only, exactly like SOLANA_PRIVATE_KEY.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _get_float(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_list(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


# =============================================================================
# LIVE TRADING -- hard-disabled at the source level, the same
# defense-in-depth pattern src/wallet.py's EXECUTION_ENABLED_IN_CODE
# uses: no environment variable can turn this on. Flipping it would
# require editing this literal line in a code review, not a config
# change -- and nothing in this project ever has, or will in this
# session. There is no order-placement code path in src/stocks at all
# yet; this flag exists so that if/when one is ever built, it starts
# from "impossible", not "off by default".
# =============================================================================
STOCKS_LIVE_TRADING = False

# --- Alpaca (broker + market data) -- optional, free paper trading and
# market data once you have an account; nothing here has ever been
# configured or connected in this project. Read src/stocks/alpaca_client.py's
# module docstring before setting these. ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_ENABLED = _get_bool("ALPACA_ENABLED", True)
# Paper trading endpoint ONLY -- this project never points at
# api.alpaca.markets (the live-money base URL), regardless of env vars.
ALPACA_TRADING_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE_URL = "https://data.alpaca.markets"
ALPACA_REQUEST_TIMEOUT_SECONDS = _get_float("ALPACA_REQUEST_TIMEOUT_SECONDS", 10)
ALPACA_REQUEST_MAX_RETRIES = _get_int("ALPACA_REQUEST_MAX_RETRIES", 3)
ALPACA_REQUEST_RETRY_BACKOFF_SECONDS = _get_float("ALPACA_REQUEST_RETRY_BACKOFF_SECONDS", 1.5)

# --- Data provider selection ---
# "auto" = use Alpaca if configured (broker-grade, real-time), else fall
# back to yfinance (free, no account needed, slightly delayed/EOD-ish
# for some endpoints) -- see src/stocks/data_provider.py. Force one
# explicitly with "alpaca" or "yfinance".
STOCKS_DATA_PROVIDER = os.getenv("STOCKS_DATA_PROVIDER", "auto").strip().lower()

# --- Universe: what the scanner considers at all ---
# A curated, liquid, well-known set of large/mid-cap US tickers spanning
# sectors -- there is no free "most active stocks today" screener
# without scraping or a paid data feed, so this project scans a fixed
# universe and ranks *within* it by live volume/volatility/momentum,
# same spirit as choosing a watchlist by hand. Override with a
# comma-separated STOCKS_UNIVERSE to scan a different set.
_DEFAULT_UNIVERSE = (
    "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,NFLX,AVGO,"
    "JPM,BAC,WFC,GS,MS,"
    "XOM,CVX,"
    "UNH,JNJ,PFE,LLY,"
    "COST,WMT,HD,NKE,SBUX,MCD,"
    "BA,CAT,GE,"
    "CRM,ORCL,ADBE,INTC,QCOM,MU,PLTR,SNOW,SHOP,UBER,ABNB,"
    "COIN,MARA,RIOT,"
    "SPY,QQQ,IWM"
)
STOCKS_UNIVERSE = _get_list("STOCKS_UNIVERSE", tuple(_DEFAULT_UNIVERSE.split(",")))

# Benchmark/regime reference symbol (broad-market ETF).
MARKET_REGIME_SYMBOL = os.getenv("MARKET_REGIME_SYMBOL", "SPY")

# --- Continuous mode ---
# Much slower than the crypto radar's 60s -- individual US equities do
# not move meaningfully minute to minute the way a brand-new meme coin
# does, and polling faster just burns API/data budget for no benefit.
STOCKS_LOOP_INTERVAL_SECONDS = _get_float("STOCKS_LOOP_INTERVAL_SECONDS", 300.0)

# --- First-pass discovery filters (what's even worth scoring) ---
STOCKS_MIN_PRICE_USD = _get_float("STOCKS_MIN_PRICE_USD", 5.0)
STOCKS_MAX_PRICE_USD = _get_float("STOCKS_MAX_PRICE_USD", 2000.0)
STOCKS_MIN_AVG_VOLUME = _get_float("STOCKS_MIN_AVG_VOLUME", 500000.0)  # 20-day avg daily volume
STOCKS_MIN_RELATIVE_VOLUME = _get_float("STOCKS_MIN_RELATIVE_VOLUME", 1.2)  # today's vol / 20d avg
STOCKS_MIN_ATR_PCT = _get_float("STOCKS_MIN_ATR_PCT", 0.8)  # ATR14 as % of price -- too quiet = skip
STOCKS_MAX_ATR_PCT = _get_float("STOCKS_MAX_ATR_PCT", 15.0)  # too wild = skip (halts/news chaos risk)
STOCKS_MAX_SPREAD_PCT = _get_float("STOCKS_MAX_SPREAD_PCT", 1.0)  # (high-low)/close on the latest bar, a liquidity proxy without a live quote feed

# --- Scoring ---
STOCKS_MIN_SCORE = _get_int("STOCKS_MIN_SCORE", 55)
# Bounded bonus X social signal can add -- additive only, never a gate,
# exactly like the crypto side (src.x_intelligence.score_bonus_for_signal).
STOCKS_X_SCORE_MAX_BONUS = _get_int("STOCKS_X_SCORE_MAX_BONUS", 8)

# --- Strategy parameters (each strategy module reads only what it needs) ---
MOMENTUM_LOOKBACK_DAYS = _get_int("MOMENTUM_LOOKBACK_DAYS", 20)
BREAKOUT_LOOKBACK_DAYS = _get_int("BREAKOUT_LOOKBACK_DAYS", 20)
MEAN_REVERSION_RSI_PERIOD = _get_int("MEAN_REVERSION_RSI_PERIOD", 14)
MEAN_REVERSION_RSI_OVERSOLD = _get_float("MEAN_REVERSION_RSI_OVERSOLD", 30.0)
MEAN_REVERSION_RSI_OVERBOUGHT = _get_float("MEAN_REVERSION_RSI_OVERBOUGHT", 70.0)
VWAP_RECLAIM_LOOKBACK_BARS = _get_int("VWAP_RECLAIM_LOOKBACK_BARS", 6)

# --- Risk engine ---
STOCKS_STARTING_CAPITAL_USD = _get_float("STOCKS_STARTING_CAPITAL_USD", 10000.0)
STOCKS_MAX_POSITION_USD = _get_float("STOCKS_MAX_POSITION_USD", 1500.0)
STOCKS_MAX_OPEN_POSITIONS = _get_int("STOCKS_MAX_OPEN_POSITIONS", 5)
STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT = _get_float("STOCKS_MAX_CAPITAL_DEPLOYMENT_PCT", 80.0)
STOCKS_MAX_DAILY_LOSS_PCT = _get_float("STOCKS_MAX_DAILY_LOSS_PCT", 3.0)
# Circuit breaker: halt new entries (existing positions still managed)
# once realized+unrealized drawdown from the peak equity reaches this.
STOCKS_MAX_DRAWDOWN_PCT = _get_float("STOCKS_MAX_DRAWDOWN_PCT", 10.0)
STOCKS_MAX_TRADES_PER_DAY = _get_int("STOCKS_MAX_TRADES_PER_DAY", 10)  # overtrading guard

# --- Exit rules -- ATR-based (volatility-adjusted), not fixed percentages ---
STOCKS_STOP_LOSS_ATR_MULT = _get_float("STOCKS_STOP_LOSS_ATR_MULT", 1.5)
# TAKE_PROFIT/TRAILING_ARM/TRAILING_STOP defaults below were revised
# from an earlier 3.0/1.5/2.0 baseline after a systematic 48-combination
# grid search (src.stocks.backtester + src.stocks.research_pipeline's
# full walk-forward/out-of-sample/regime rigor -- not just eyeballing
# the aggregate number) run once the backtester started actually
# simulating the trailing stop (see backtester.py's module docstring on
# why that fidelity fix mattered): the original 2.0-ATR trail cut
# winners short before they could reach a 3.0-ATR target often enough
# to measurably hurt out-of-sample quality. This wider combination
# (breakout, the active strategy: out-of-sample PF 1.48->1.66,
# expectancy 0.75%->1.13%, return-to-drawdown ratio 1.43->5.12, combined
# drawdown 129%->99% -- better on every axis, not a one-metric trade-off)
# was chosen from the results that were BOTH LIVE_CANDIDATE-qualifying
# AND held up across strategies other than the one being tuned on
# (momentum's own numbers independently improved with the same change,
# evidence this isn't overfit to breakout specifically) -- see
# STOCKS_LIVE_READINESS_REPORT.md for the full comparison.
STOCKS_TAKE_PROFIT_ATR_MULT = _get_float("STOCKS_TAKE_PROFIT_ATR_MULT", 4.0)
STOCKS_TRAILING_STOP_ATR_MULT = _get_float("STOCKS_TRAILING_STOP_ATR_MULT", 3.0)
# Trailing stop only arms once price has moved this many ATRs in favor
# (avoids trailing a position that hasn't even proven itself yet).
STOCKS_TRAILING_ARM_ATR_MULT = _get_float("STOCKS_TRAILING_ARM_ATR_MULT", 1.0)
STOCKS_MAX_HOLDING_DAYS = _get_float("STOCKS_MAX_HOLDING_DAYS", 10.0)

# --- Backtesting ---
BACKTEST_LOOKBACK_DAYS = _get_int("BACKTEST_LOOKBACK_DAYS", 730)  # ~2y of daily bars -- used by the live loop's periodic learning check (src.stocks.learning_engine), kept short so that runs fast every few hours
BACKTEST_IN_SAMPLE_FRACTION = _get_float("BACKTEST_IN_SAMPLE_FRACTION", 0.7)  # first 70% = in-sample/tune, last 30% = out-of-sample/validate
BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE = _get_int("BACKTEST_MIN_TRADES_FOR_SIGNIFICANCE", 20)
# How many symbols to backtest concurrently (src.stocks.backtester) --
# yfinance calls are I/O-bound and pandas releases the GIL during most
# of its C-level work, so threads (not processes, which would need
# every strategy module to be picklable across a process boundary) give
# a real wall-clock win without any of that complexity.
BACKTEST_MAX_WORKERS = _get_int("BACKTEST_MAX_WORKERS", 8)

# --- Historical research pipeline (src/stocks/research_pipeline.py) ---
# A much deeper lookback than the live loop's periodic check above --
# this is what turns "a couple hundred backtest trades" into thousands
# across the universe, the actual point of a large-scale historical
# study. yfinance serves ~10 years of daily bars for established large/
# mid-caps for free, so this asks for that without needing a paid data
# vendor.
RESEARCH_LOOKBACK_DAYS = _get_int("RESEARCH_LOOKBACK_DAYS", 3650)  # ~10y
# How many (roughly equal, calendar-order) folds to split each symbol's
# trade history into for walk-forward-style robustness reporting -- see
# research_pipeline.py's own docstring for why this is done by bucketing
# already-simulated trades by entry date rather than re-running the
# simulation per fold (a single causal, no-lookahead pass already
# produces every trade; which calendar period a completed trade's entry
# falls into is just a label on it afterward).
RESEARCH_WALK_FORWARD_FOLDS = _get_int("RESEARCH_WALK_FORWARD_FOLDS", 5)

# --- Bar cache (src/stocks/bar_cache.py) ---
# Daily bars only change once a day (at market close) -- there is no
# reason a research run started twice in the same day should ever
# refetch the same symbol's history from yfinance/Alpaca.
STOCKS_BAR_CACHE_TTL_HOURS = _get_float("STOCKS_BAR_CACHE_TTL_HOURS", 20.0)

# --- Realistic trading costs (src/stocks/backtester.py) ---
# Applied to every simulated fill (backtest AND, via risk_engine's
# position sizing being unaffected but paper_broker recording the same
# constants for consistency, paper trading) so historical results
# aren't flattering an execution that couldn't really be achieved.
# Slippage: the fill price moves against the trader by this many basis
# points from the reference price (next bar's open on entry, this bar's
# stop/target level on exit) -- a conservative, symmetric estimate for
# the liquid large/mid-cap universe this project trades; it is NOT a
# substitute for a real market-impact model, just a floor under
# "assume you get the exact printed price for free", which no real fill
# ever does.
STOCKS_SLIPPAGE_BPS = _get_float("STOCKS_SLIPPAGE_BPS", 5.0)  # 0.05%
# Alpaca's paper/live equities trading is commission-free (as of this
# writing) -- 0.0 here reflects that real, current fact about the
# specific broker this project targets, not an assumption that trading
# is free in general. Overridable if that ever changes or a different
# broker is swapped in.
STOCKS_COMMISSION_PER_TRADE_USD = _get_float("STOCKS_COMMISSION_PER_TRADE_USD", 0.0)

# --- Market hours gating (src/stocks/market_hours.py) ---
# Skip full scan cycles while the US market is closed (nights/weekends/
# holidays) instead of burning data-provider calls on data that hasn't
# moved -- see market_hours.py's own docstring for exactly what this
# does and does not model (regular session only, a fixed federal-holiday
# list, no early-close half-days).
STOCKS_RESPECT_MARKET_HOURS = _get_bool("STOCKS_RESPECT_MARKET_HOURS", True)
# How often to re-check "is the market open yet" while waiting through a
# closed period -- deliberately much coarser than STOCKS_LOOP_INTERVAL_
# SECONDS, since nothing changes minute to minute overnight.
STOCKS_MARKET_CLOSED_POLL_SECONDS = _get_float("STOCKS_MARKET_CLOSED_POLL_SECONDS", 300.0)

# --- Health / auto-recovery (src/stocks/health.py) ---
STOCKS_HEALTH_FILE_NAME = "health_status.json"
# Exponential backoff for a cycle that raised (data provider down, rate
# limited, network timeout, etc.) -- same shape as alpaca_client's own
# per-request backoff, but at the whole-cycle level.
STOCKS_RECOVERY_BACKOFF_BASE_SECONDS = _get_float("STOCKS_RECOVERY_BACKOFF_BASE_SECONDS", 30.0)
STOCKS_RECOVERY_BACKOFF_MAX_SECONDS = _get_float("STOCKS_RECOVERY_BACKOFF_MAX_SECONDS", 1800.0)  # cap at 30 min between retries

# --- Self-learning loop (src/stocks/learning_engine.py) ---
# NOTE on what this gates: the historical-backtest-driven candidate
# search (comparing every strategy's fresh out-of-sample numbers) needs
# NO live paper trades at all -- it's pure backtest, gated only by
# STOCKS_LEARNING_CHECK_INTERVAL_SECONDS below so it doesn't re-run
# every 5-minute cycle. STOCKS_LEARNING_MIN_NEW_TRADES/_ROLLBACK_MIN_
# TRADES below gate ONLY the separate rollback check, which by
# definition needs the active strategy's own live results to judge --
# there is no backtest substitute for "how did this strategy actually
# perform once real (paper) money was on it". Never wait on live trade
# count before benefiting from historical evidence.
STOCKS_LEARNING_MIN_NEW_TRADES = _get_int("STOCKS_LEARNING_MIN_NEW_TRADES", 15)
# Minimum wall-clock time between learning runs regardless of trade
# count -- re-backtesting every strategy is a real (if free) cost in
# time/network calls, and strategy quality doesn't meaningfully change
# hour to hour.
STOCKS_LEARNING_CHECK_INTERVAL_SECONDS = _get_float("STOCKS_LEARNING_CHECK_INTERVAL_SECONDS", 21600.0)  # 6h
# A candidate must beat the active strategy's out-of-sample profit
# factor by at least this many percentage points (not just nominally)
# to be adopted -- guards against swapping strategies over noise.
STOCKS_LEARNING_MIN_PF_IMPROVEMENT = _get_float("STOCKS_LEARNING_MIN_PF_IMPROVEMENT", 0.1)
# If the currently active strategy's OWN live paper trades (since it was
# activated) accumulate to at least this many and its expectancy turns
# negative, roll back to the previously active strategy automatically.
STOCKS_LEARNING_ROLLBACK_MIN_TRADES = _get_int("STOCKS_LEARNING_ROLLBACK_MIN_TRADES", 20)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
