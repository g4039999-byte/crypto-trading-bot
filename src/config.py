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
RADAR_LOOP_INTERVAL_SECONDS = _get_float("RADAR_LOOP_INTERVAL_SECONDS", 300)

# Logging.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Optional, unused today -- reserved for when DexScreener (or a replacement
# data source) requires authentication. Never hard-code a real value here.
DEXSCREENER_API_KEY = os.getenv("DEXSCREENER_API_KEY")


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

# --- Wallet & RPC -- SOLANA_PRIVATE_KEY must NEVER be committed, logged,
# printed, or pasted into a chat. It is read from the environment only.
# SOLANA_WALLET_PUBLIC_KEY is not secret (it's just an address) and is
# safe to keep in .env or share for read-only balance checks.
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WALLET_PUBLIC_KEY = os.getenv("SOLANA_WALLET_PUBLIC_KEY", "")
SOLANA_PRIVATE_KEY = os.getenv("SOLANA_PRIVATE_KEY", "")

# --- Jupiter aggregator (public endpoints, no key required) ---
JUPITER_QUOTE_URL = os.getenv("JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
JUPITER_SWAP_URL = os.getenv("JUPITER_SWAP_URL", "https://quote-api.jup.ag/v6/swap")
SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
