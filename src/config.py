"""Central configuration for the radar.

All values have safe defaults that match the project's original behavior.
Anything here can be overridden via environment variables (typically loaded
from a local .env file that is NOT committed to git -- see .env.example).

No secrets live in this file or in git. The DexScreener endpoints used by
this project are public and do not require an API key today, but the
loader below is ready for one (DEXSCREENER_API_KEY) in case that changes.
"""

import os

from dotenv import load_dotenv

# Loads variables from a local .env file if present. Safe to call even if
# the file does not exist (nothing happens).
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


# --- Filtering thresholds (unchanged defaults from the original project) ---
MIN_LIQUIDITY_USD = _get_float("MIN_LIQUIDITY_USD", 5000)
MIN_VOLUME_24H_USD = _get_float("MIN_VOLUME_24H_USD", 25000)
MIN_BUY_SELL_RATIO = _get_float("MIN_BUY_SELL_RATIO", 0.8)

# Not wired into the first-pass filter yet (kept as-is from the original
# project so filtering behavior is unchanged). Reserved for a future
# "reject if sells are overwhelmingly dominant" check.
MAX_SELL_BUY_RATIO = _get_float("MAX_SELL_BUY_RATIO", 3.0)

# --- Networking / operational settings (new, all optional) ---
REQUEST_TIMEOUT_SECONDS = _get_float("REQUEST_TIMEOUT_SECONDS", 10)
REQUEST_MAX_RETRIES = _get_int("REQUEST_MAX_RETRIES", 3)
REQUEST_RETRY_BACKOFF_SECONDS = _get_float("REQUEST_RETRY_BACKOFF_SECONDS", 1.5)

# DexScreener's /tokens/v1 endpoint accepts at most 30 addresses per call.
MAX_ADDRESSES_PER_REQUEST = _get_int("MAX_ADDRESSES_PER_REQUEST", 30)

# How many historical snapshots to keep per token (data/snapshots.json).
SNAPSHOT_HISTORY_LIMIT = _get_int("SNAPSHOT_HISTORY_LIMIT", 60)

# How many previously-seen tokens to keep re-checking each cycle (on top
# of whatever DexScreener's "latest profiles" feed returns), so a token
# keeps accumulating snapshots -- and observation.py can report a real
# trend -- even after it drops out of that feed. 0 disables the
# watchlist and restores the original behavior (newly-discovered tokens
# only).
RADAR_WATCHLIST_SIZE = _get_int("RADAR_WATCHLIST_SIZE", 100)

# --- Continuous mode (`python -m src.radar --loop`) ---
# 60s (was 300s): meme coins move fast, and a token needs 2 cycles of
# snapshots before observation.py can report a real trend at all (see
# src/observation.py) -- at 300s that meant 5-10 real minutes before a
# brand-new token could ever qualify for a trend-gated entry, on top of
# whatever time it takes to notice it in the first place. 60s keeps each
# cycle's own request volume unchanged (same batches of MAX_ADDRESSES_
# PER_REQUEST) while cutting that discovery-to-decision latency roughly
# 5x; DexScreener's public feed comfortably tolerates this cadence.
RADAR_LOOP_INTERVAL_SECONDS = _get_float("RADAR_LOOP_INTERVAL_SECONDS", 60)

# Logging.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Optional, unused today -- reserved for when DexScreener (or a replacement
# data source) requires authentication. Never hard-code a real value here.
DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY")

# --- Pump.fun discovery (src/pumpfun_client.py) -- optional, additive-only
# second discovery feed alongside DexScreener's "latest profiles" feed in
# src/radar.py. Pump.fun has no free/official discovery API of its own;
# this uses Solana Tracker's Data API (https://docs.solanatracker.io/data-api,
# free tier: 2500 requests/month), filtered to market=pumpfun. Leaving
# PUMPFUN_API_KEY empty costs nothing and makes zero network calls --
# src/pumpfun_client.is_configured() gates every function on it, exactly
# like ALPACA_API_KEY/X_BEARER_TOKEN elsewhere in this project. This feed
# only ever contributes token ADDRESSES to the radar's existing discovery
# list -- src/dex_client.py remains the only source of the actual
# liquidity/volume/txns data used for scoring, so a Pump.fun-discovered
# address that DexScreener has not indexed yet simply yields no pair data
# this cycle (same as any other momentarily-unindexed address already).
PUMPFUN_API_KEY = os.getenv("PUMPFUN_API_KEY", "")
PUMPFUN_ENABLED = _get_bool("PUMPFUN_ENABLED", True)
PUMPFUN_BASE_URL = "https://data.solanatracker.io"
PUMPFUN_DISCOVERY_LIMIT = _get_int("PUMPFUN_DISCOVERY_LIMIT", 50)


# =============================================================================
# LIVE TRADING -- disabled by default. Read this whole block before changing
# anything.
#
# LIVE_TRADING=false is the ONLY value that has ever been tested or run.
# Flipping it to true is a real-money action on a real Solana wallet.
# src/kill_switch.py is what actually enforces these gates at runtime; this
# section only defines the values it reads. See README.md's "Live trading"
# section for the full checklist before ever setting LIVE_TRADING=true.
# =============================================================================

# Master switch. Stays false until a human deliberately turns it on in a
# LOCAL .env file that is never committed. The code additionally refuses
# to place a real order unless CONFIRM_LIVE_TRADING (below) also matches
# exactly, and unless the kill-switch file (below) is absent.
LIVE_TRADING = _get_bool("LIVE_TRADING", False)

# A second, independent gate. Even with LIVE_TRADING=true, no real order
# is sent unless this env var equals this exact phrase. This exists so
# that a single accidental "LIVE_TRADING=true" in a copy-pasted .env can
# never be enough, on its own, to spend real money.
REQUIRED_CONFIRM_PHRASE = "I_UNDERSTAND_AND_APPROVE_LIVE_TRADING"
CONFIRM_LIVE_TRADING = os.getenv("CONFIRM_LIVE_TRADING", "")

# Immediate kill switch: if this file exists, no new trade is placed,
# checked fresh before every single decision (no restart needed). Create
# it by hand at any time with: touch data/STOP_TRADING
KILL_SWITCH_FILE = os.getenv("KILL_SWITCH_FILE", "data/STOP_TRADING")

# --- Capital & position sizing -- keep these true to your real balance ---
# Update this whenever the wallet's real balance changes; it is only used
# for position-size math, never fetched automatically from the chain in
# this version.
TOTAL_CAPITAL_USD = _get_float("TOTAL_CAPITAL_USD", 24.0)

# Hard ceiling on a single trade, in USD. Position sizing (src/portfolio.py)
# never exceeds this, regardless of score or confidence.
MAX_TRADE_USD = _get_float("MAX_TRADE_USD", 5.0)

# Only ever hold this many open positions at once. Starts at 1 on purpose:
# with ~$24 of capital, spreading across multiple meme tokens at once adds
# risk (more slippage, more things to monitor) without meaningfully
# reducing it.
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 1)

# Stop trading for the day once realized losses reach this % of
# TOTAL_CAPITAL_USD. src/portfolio.py tracks realized PnL per UTC day.
MAX_DAILY_LOSS_PCT = _get_float("MAX_DAILY_LOSS_PCT", 20.0)

# Never deploy the whole wallet: this caps total open position size as a
# % of TOTAL_CAPITAL_USD, always leaving a reserve for Solana network
# fees (every transaction needs a small amount of native SOL) and to
# avoid ever being fully exposed.
MAX_CAPITAL_DEPLOYMENT_PCT = _get_float("MAX_CAPITAL_DEPLOYMENT_PCT", 80.0)

# --- Exit rules ---
STOP_LOSS_PCT = _get_float("STOP_LOSS_PCT", 25.0)     # sell if price drops this % from entry
TAKE_PROFIT_PCT = _get_float("TAKE_PROFIT_PCT", 50.0)  # sell if price rises this % from entry

# Close a position after this long regardless of price, so every trade
# reaches a decision instead of being held indefinitely (a meme coin that
# has gone flat is a decision to move on, not a reason to keep watching
# forever). Applies to both src/portfolio.py and src/paper_portfolio.py's
# check_exit -- this only changes exit *logic*, not whether live trading
# is allowed to run at all (LIVE_TRADING still gates that separately).
MAX_HOLDING_MINUTES = _get_float("MAX_HOLDING_MINUTES", 240.0)

# --- Execution safety ---
# Maximum slippage tolerance passed to Jupiter, in basis points (100 = 1%).
# If the quote cannot be filled within this bound the swap is not sent.
MAX_SLIPPAGE_BPS = _get_int("MAX_SLIPPAGE_BPS", 300)

# --- Entry screening (stricter than the general radar filter in
#     MIN_LIQUIDITY_USD / MIN_VOLUME_24H_USD above, on purpose: those
#     control what gets *scored and shown*, these control what real money
#     is allowed to touch) ---
MIN_LIVE_SCORE = _get_int("MIN_LIVE_SCORE", 80)
MIN_LIVE_LIQUIDITY_USD = _get_float("MIN_LIVE_LIQUIDITY_USD", 15000)
MIN_LIVE_VOLUME_24H_USD = _get_float("MIN_LIVE_VOLUME_24H_USD", 50000)

# Avoid the first few minutes of a pair's life (the highest-risk window
# for rug pulls) and avoid pairs already well past their momentum window.
MIN_LIVE_PAIR_AGE_MINUTES = _get_float("MIN_LIVE_PAIR_AGE_MINUTES", 5)
MAX_LIVE_PAIR_AGE_MINUTES = _get_float("MAX_LIVE_PAIR_AGE_MINUTES", 180)

# Tiny probe amount, in SOL, used to test a round trip (buy quote then
# sell quote back to SOL) before ever actually buying -- a cheap defense
# against tokens that can be bought but not sold ("honeypots"). 0.01 SOL
# is small enough not to matter if the round trip itself is ever executed
# for real, which it currently is not (see src/jupiter_client.py).
SELLABILITY_PROBE_SOL = _get_float("SELLABILITY_PROBE_SOL", 0.01)

# Reject a candidate if a simulated buy-then-sell round trip would lose
# more than this percentage, even before fees -- a large gap is a strong
# signal of a high sell tax or a honeypot.
MAX_ROUND_TRIP_LOSS_PCT = _get_float("MAX_ROUND_TRIP_LOSS_PCT", 20.0)

# --- Paper-trading entry tuning -- deliberately separate from, and more
#     permissive than, MIN_LIVE_* above. MIN_LIVE_SCORE=80 alone is close
#     to the scoring formula's theoretical ceiling (base*0.60 + momentum*0.40
#     tops out around 90) and was never once reached in practice: a full
#     day of real radar data (119 tracked opportunities, 2026-09-03) topped
#     out at 67. Combined with requiring trend in (STRONG, RISING) -- only
#     ~4% of that same sample -- paper trading was mathematically almost
#     guaranteed to never place a single trade, independent of any bug.
#     These values open that up to a "reasonable, not perfect" opportunity
#     (roughly the top 5% by score in that sample) while every safety
#     screen below (liquidity/volume floor, rug-window age, honeypot/
#     sellability check) still applies in full -- see src/paper_trader.py
#     and src/risk.py. None of this touches MIN_LIVE_* or live_trader.py:
#     live trading is unaffected and stays fully gated by LIVE_TRADING.
PAPER_MIN_SCORE = _get_int("PAPER_MIN_SCORE", 40)
PAPER_ENTRY_TRENDS = tuple(
    t.strip() for t in os.getenv("PAPER_ENTRY_TRENDS", "STRONG,RISING,NEUTRAL").split(",") if t.strip()
)

# 2026-09-04: scripts/diagnose_paper_strategy.py found -- checked against
# all 4 walk-forward folds, not just the aggregate -- that RISING/STRONG
# trend entries (src.observation.compute_trend's classification, a
# short-term buy-FLOW *delta* between consecutive snapshots) are
# fold-consistently unprofitable while NEUTRAL is fold-consistently
# profitable/flat, i.e. this signal tends to buy a token right after it
# has already spiked rather than catching sustainable early momentum.
# scripts/backtest_paper_strategy.py then tested 8 different mechanisms
# (full/partial exclusion, this stricter score bar, a persistence
# confirmation, two velocity caps) against real historical data with the
# same out-of-sample/walk-forward-fold/coin-group discipline; this one
# ("weight reduction" rather than deleting RISING/STRONG outright) was
# the only one that improved EVERY axis simultaneously vs the deployed
# baseline -- full-dataset profit_factor 0.72->0.84, max drawdown
# 1071->672pp, out-of-sample profit_factor 1.44->3.27, out-of-sample
# expectancy +7.83%->+20.97%, fold_stability 0.25->0.5 (n=113/7 full/OOS
# trades -- still a small sample; keep re-validating as more radar data
# accumulates). RISING itself stayed weak even at the higher bar (this
# raises it, doesn't fully fix it -- see the module docstring notes in
# backtest_paper_strategy.py); STRONG improved meaningfully. PAPER_MIN_SCORE
# above is UNCHANGED (still 40, still applies to NEUTRAL) -- this is an
# ADDITIONAL, higher bar that only RISING/STRONG entries must also clear.
PAPER_ELEVATED_TREND_MIN_SCORE = _get_int("PAPER_ELEVATED_TREND_MIN_SCORE", 55)

# Liquidity/volume/age safety floors for paper trades -- reuses the
# general radar filter's liquidity/volume bar (MIN_LIQUIDITY_USD /
# MIN_VOLUME_24H_USD above: "worth looking at" at all) rather than the
# extra-strict MIN_LIVE_* bar meant for real money, while keeping the
# same rug-window/staleness age window and full honeypot/sellability
# screening as live (src/risk.py, src/jupiter_client.py) untouched.
PAPER_MIN_LIQUIDITY_USD = _get_float("PAPER_MIN_LIQUIDITY_USD", MIN_LIQUIDITY_USD)
PAPER_MIN_VOLUME_24H_USD = _get_float("PAPER_MIN_VOLUME_24H_USD", MIN_VOLUME_24H_USD)

# 15m (was MIN_LIVE_PAIR_AGE_MINUTES=5m): the first paper-trading session
# (2026-09-03, 20 closed trades under the 5m floor) lost -$21.99 at a 20%
# win rate. scripts/backtest_paper_strategy.py replayed the same rules
# against every token this radar has ever recorded (161 tokens/~7000
# snapshots) and swept the age floor alongside stop/take-profit: entries
# taken right at the 5m floor were disproportionately losers (early-life
# pump-then-dump), while the *same* candidates re-evaluated a bit older
# were consistently better -- PnL turned positive and win rate roughly
# quadrupled once entries were required to be >=15m old, with results
# essentially flat between 15-20m. This is a paper-only tuning; live's
# own MIN_LIVE_PAIR_AGE_MINUTES=5 is untouched.
PAPER_MIN_PAIR_AGE_MINUTES = _get_float("PAPER_MIN_PAIR_AGE_MINUTES", 15.0)
PAPER_MAX_PAIR_AGE_MINUTES = _get_float("PAPER_MAX_PAIR_AGE_MINUTES", MAX_LIVE_PAIR_AGE_MINUTES)

# Paper trading may hold more than one position at once (real risk is
# zero), unlike the live MAX_OPEN_POSITIONS=1 above which is deliberately
# conservative with real capital.
PAPER_MAX_OPEN_POSITIONS = _get_int("PAPER_MAX_OPEN_POSITIONS", 3)

# --- Paper-only exit tuning -- kept separate from the shared STOP_LOSS_
# PCT/TAKE_PROFIT_PCT above so live_trader.py's numbers are never touched
# by this. Same backtest as above: a tighter take-profit (25% instead of
# 50%) locks in gains before these thin-liquidity tokens give them back --
# in the same historical replay, the CURRENT rules (5m floor, 25/50
# stop/take) produced -$21.99 over 20 trades (20% win rate); the
# CANDIDATE rules below (15m floor, 25/25 stop/take, the liquidity-
# drawdown guard and cooldown just below) produced +$8.12 over 7 trades
# (85.7% win rate) on the exact same data. Stop-loss itself (25%) was
# NOT the problem -- it already outperformed tighter 15/20% alternatives
# in the sweep -- so it is carried over unchanged.
PAPER_STOP_LOSS_PCT = _get_float("PAPER_STOP_LOSS_PCT", 25.0)
PAPER_TAKE_PROFIT_PCT = _get_float("PAPER_TAKE_PROFIT_PCT", 25.0)

# Reject a candidate whose liquidity has already fallen this much from
# its own recent peak (across the snapshots the radar has for it so
# far), even if its current liquidity still clears PAPER_MIN_LIQUIDITY_
# USD -- a level "still above the floor" can hide a pool actively being
# drained. Both real closing losses on 2026-09-03 (NEVER, Magachud) had
# already lost >50% of peak liquidity by the time they were bought.
# 40 (was 25): re-swept after adding the 15m age floor above -- at 25%
# this guard was also rejecting a lot of ordinary volatility that had
# nothing to do with draining, for no extra protection (both real
# incidents it targets are >50% drawdowns, comfortably still caught at
# 40%). Backtest on the same 173-token dataset: 25% -> 10 trades/60%
# win/+$3.73; 40% -> 12 trades/58.3% win/+$5.97 -- more trades, better
# total PnL, essentially the same win rate.
PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT = _get_float("PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT", 40.0)

# Do not re-buy a token for this long after a stop-loss on it -- observed
# live on 2026-09-03: Magachud was stop-lossed, then bought again 5
# minutes later while still in the same decline, and lost a second time.
# 0 disables the cooldown.
PAPER_STOP_LOSS_COOLDOWN_MINUTES = _get_float("PAPER_STOP_LOSS_COOLDOWN_MINUTES", 60.0)

# --- Wallet & RPC -- SOLANA_PRIVATE_KEY must NEVER be committed, logged,
# printed, or pasted into a chat. It is read from the environment only.
# SOLANA_WALLET_PUBLIC_KEY is not secret (it's just an address) and is
# safe to keep in .env or share for read-only balance checks.
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WALLET_PUBLIC_KEY = os.getenv("SOLANA_WALLET_PUBLIC_KEY", "")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

# --- Jupiter aggregator (public "lite" tier, no key required) ---
# quote-api.jup.ag (V6) was deprecated on 2025-10-01; it no longer even
# resolves in DNS (confirmed 2026-09-03). lite-api.jup.ag is Jupiter's
# current free/no-API-key tier of the same Swap API (verified live: a
# real quote request against it returns HTTP 200 with the same
# outAmount-bearing response shape jupiter_client.py already expects).
# Without this, round_trip_check()'s honeypot/sellability screening can
# never succeed for anyone, live or paper -- every candidate that
# otherwise passes score/trend/risk would still be skipped with "no buy
# route available", not because it is unsafe, but because the quote
# request itself could never reach a live endpoint.
JUPITER_QUOTE_URL = os.getenv("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")
JUPITER_SWAP_URL = os.getenv("JUPITER_SWAP_URL", "https://lite-api.jup.ag/swap/v1/swap")
SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
# Circle's official mainnet USDC mint -- used only to derive an implied
# SOL/USD price via a live Jupiter quote (SOL -> USDC), for sizing a real
# order in SOL terms from a USD cap. Not secret, not user-specific.
USDC_MINT_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# --- Real execution tuning -- only relevant once a human has manually
# flipped src.wallet.EXECUTION_ENABLED_IN_CODE to True (see that file);
# these have no effect before then. ---
# How long to wait for on-chain confirmation after sending a real swap
# before giving up and returning a "timed_out" (not "failed") result.
SWAP_CONFIRMATION_TIMEOUT_SECONDS = _get_float("SWAP_CONFIRMATION_TIMEOUT_SECONDS", 60)
SWAP_CONFIRMATION_POLL_SECONDS = _get_float("SWAP_CONFIRMATION_POLL_SECONDS", 2)


# =============================================================================
# X (Twitter) social intelligence -- optional, additive, and OFF unless a
# bearer token is explicitly configured. Read this before setting X_BEARER_
# TOKEN: as of 2026, X's API has no free tier for a new project -- reads
# are billed pay-per-use (~$0.005/post, or a legacy $200+/month
# subscription for accounts that already had one). This module makes zero
# network calls and costs nothing until X_BEARER_TOKEN is set; nothing in
# this project has ever set it or spent anything against a real X account.
# X_BEARER_TOKEN must NEVER be committed, logged, printed, or pasted into
# a chat -- it is read from the environment only, exactly like
# SOLANA_PRIVATE_KEY above.
# =============================================================================

X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")

# Explicit override; normally left unset so "enabled" just means "a
# bearer token is configured" (see src/x_client.py's is_configured()).
# Set to false to keep the token configured but the feature off.
X_ENABLED = _get_bool("X_ENABLED", True)

X_API_BASE_URL = os.getenv("X_API_BASE_URL", "https://api.x.com/2")
X_REQUEST_TIMEOUT_SECONDS = _get_float("X_REQUEST_TIMEOUT_SECONDS", 10)
X_REQUEST_MAX_RETRIES = _get_int("X_REQUEST_MAX_RETRIES", 3)
X_REQUEST_RETRY_BACKOFF_SECONDS = _get_float("X_REQUEST_RETRY_BACKOFF_SECONDS", 2.0)
# Cap on how long a single call will sleep for a rate-limit (429) reset
# before giving up on *this* cycle -- long enough to ride out a normal
# 15-minute window's reset without hammering it, short enough to never
# stall the radar loop for an entire cycle. It always retries again
# next cycle regardless -- see src/x_intelligence.py.
X_RATE_LIMIT_MAX_WAIT_SECONDS = _get_float("X_RATE_LIMIT_MAX_WAIT_SECONDS", 30.0)

# Hard daily spending guard, in number of post-reads (data/x_usage.json
# tracks the running total per UTC day). At the pay-per-use rate above,
# 500 reads/day is roughly $2.50/day worst case. Sized deliberately low
# by default -- raise it deliberately, knowing the real cost, not by
# accident. Once hit, X calls short-circuit to "no signal" for the rest
# of the day and resume automatically at UTC midnight.
X_MAX_READS_PER_DAY = _get_int("X_MAX_READS_PER_DAY", 500)

# How often the radar polls X for new signals -- deliberately much
# slower than RADAR_LOOP_INTERVAL_SECONDS itself (a search call is
# billed per post *returned*, not per call, but polling every radar
# cycle would still burn through X_MAX_READS_PER_DAY in minutes for no
# real benefit -- meme velocity is measured in minutes, not seconds).
X_POLL_INTERVAL_SECONDS = _get_float("X_POLL_INTERVAL_SECONDS", 300.0)
X_MAX_RESULTS_PER_QUERY = _get_int("X_MAX_RESULTS_PER_QUERY", 30)

# What to search for -- crypto/meme-coin-relevant terms. Comma-separated;
# each is one billed search (each returning up to X_MAX_RESULTS_PER_QUERY
# reads), run once per X_POLL_INTERVAL_SECONDS.
X_SEARCH_QUERIES = tuple(
    q.strip() for q in os.getenv(
        "X_SEARCH_QUERIES",
        "solana meme coin,new solana gem,$SOL pump,solana launch"
    ).split(",") if q.strip()
)

# A trend cluster needs at least this many *independent* authors
# mentioning it before it is considered a real signal rather than noise
# from a single account (which could be one spammer, not a trend).
X_MIN_INDEPENDENT_MENTIONS = _get_int("X_MIN_INDEPENDENT_MENTIONS", 2)

# How long a detected trend cluster stays "active" (eligible to
# correlate with a token and contribute to scoring) without a fresh
# mention -- memes move fast; a cluster that has gone quiet this long is
# stale, not a live signal.
X_SIGNAL_TTL_MINUTES = _get_float("X_SIGNAL_TTL_MINUTES", 60.0)

# Upper bound on how many points a strong X signal can add to a token's
# score (src/scoring.calculate_score's social_bonus parameter) -- an
# addition on top of the market-data score, never a substitute for it,
# and never a gate: a token with zero X signal is scored exactly as
# before this feature existed.
X_SCORE_MAX_BONUS = _get_int("X_SCORE_MAX_BONUS", 10)

# Two entity names within this difflib.SequenceMatcher similarity (0-1,
# see src/x_correlation.py) are treated as "suspiciously similar" for
# clone detection -- e.g. "$PEPITO" vs "$PEPIT0" (an O/0 swap, a classic
# typosquat) trying to ride an existing symbol's wave. 0.8: chosen so a
# single-character homoglyph swap on a 5-6 char symbol (ratio ~0.8-0.83)
# is caught, while genuinely unrelated symbols (ratio well under 0.3 in
# practice) are not.
X_CLONE_SYMBOL_SIMILARITY_THRESHOLD = _get_float("X_CLONE_SYMBOL_SIMILARITY_THRESHOLD", 0.8)
