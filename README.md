# Crypto Trading Bot -- Solana Radar

A radar that discovers newly-listed Solana meme tokens on [DexScreener](https://dexscreener.com),
scores them, tracks how they change over time, and prints a ranked list.

**No real trade has ever been placed by this project.** The radar
(discovery/analysis) is fully working and confirmed against the live
DexScreener API, can run continuously (`--loop`), and now has a **paper
trading** mode (`--paper`) that rehearses full buy/sell cycles with
simulated funds under the same rules real trading would use. A
live-trading *safety layer* also exists (risk screening, position sizing,
stop-loss/take-profit, a kill switch, decision logging) but real order
sending is not implemented and is disabled by every gate it has, by
default -- see "Live trading" below for exactly what is and is not done.

## How it works

For every candidate Solana pair, the radar runs it through a pipeline:

1. **Discover** -- `src/dex_client.py` pulls the latest token profiles from
   DexScreener, then fetches market data (liquidity, volume, trades,
   price change) for each Solana address, in batches.
2. **First-pass filter** -- reject pairs below the liquidity/volume
   thresholds or with weak buy pressure (`src/config.py`).
3. **Score** -- `src/scoring.py` (0-100, based on liquidity, volume, buy
   pressure, momentum and pair age) and `src/momentum.py` (0-75, a
   faster-moving momentum-only score) are combined into a final score
   (60% base score + 40% momentum).
4. **Stage** -- `src/stage.py` classifies the pair's age as
   `EARLY` / `RISING` / `MATURE` / `LATE`.
5. **Snapshot** -- `src/snapshot.py` appends the pair's current state to
   `data/snapshots.json` (per-token history, capped at the last N entries).
6. **Observation** -- `src/observation.py` compares the two most recent
   snapshots for a token to report a short-term trend
   (`STRONG` / `RISING` / `NEUTRAL` / `WEAK`, or `INSUFFICIENT_DATA` the
   first time a token is seen).

`src/radar.py` wires all of the above together (`evaluate_pair` /
`run_radar`) and prints the ranked results.

Each cycle queries two sets of addresses, merged: whatever DexScreener's
"latest profiles" feed discovers *this* cycle, plus a watchlist of up to
`RADAR_WATCHLIST_SIZE` (default 100) previously-seen addresses pulled
from `data/snapshots.json` (`snapshot.known_addresses()`). Without the
watchlist, a token would typically get exactly one snapshot -- the feed
moves on to newer tokens fast -- and `observation.py` would report
`INSUFFICIENT_DATA` forever. With it, a token keeps accumulating
snapshots across cycles even after it drops out of "latest", so a real
trend (`STRONG`/`RISING`/`NEUTRAL`/`WEAK`) shows up from its second
sighting onward.

## Project layout

```
crypto-trading-bot/
├── data/
│   ├── snapshots.json          # per-token history the radar reads/writes
│   ├── positions.json          # live-trading state (git-ignored, created at runtime)
│   ├── trade_log.jsonl         # every LIVE trading decision (git-ignored)
│   ├── paper_positions.json    # paper-trading state (git-ignored, created at runtime)
│   └── paper_trade_log.jsonl   # every PAPER trading decision (git-ignored)
├── src/
│   ├── config.py           # thresholds + operational settings (env-overridable)
│   ├── dex_client.py       # all network calls to DexScreener, with retries
│   ├── logging_config.py   # shared logging setup
│   ├── utils.py            # safe_get() -- null-safe nested dict access
│   ├── momentum.py         # momentum score (0-75)
│   ├── scoring.py          # base score (0-100)
│   ├── stage.py            # pair-age classification
│   ├── snapshot.py         # snapshot persistence + known_addresses() watchlist
│   ├── observation.py      # trend detection from snapshot history
│   ├── radar.py            # discovery/analysis pipeline + entry point (single run, --loop, --paper)
│   │
│   ├── paper_portfolio.py  # PAPER position sizing/tracking -- data/paper_positions.json only
│   ├── paper_logger.py     # writes data/paper_trade_log.jsonl
│   ├── paper_trader.py     # PAPER entry/exit decisions -- no wallet, no kill switch, no real risk
│   │
│   ├── kill_switch.py      # the one choke point every live-trading path must pass
│   ├── jupiter_client.py   # read-only Jupiter quotes + honeypot/round-trip check
│   ├── risk.py             # pre-trade safety screening (stricter than the radar filter)
│   ├── portfolio.py        # position sizing, stop-loss/take-profit, daily loss cap
│   ├── trade_logger.py     # writes data/trade_log.jsonl
│   ├── live_trader.py      # entry/exit decision logic -- logs decisions, places no order
│   └── wallet.py           # wallet loading + connection test; real signing NOT implemented
├── tests/                  # unit + integration tests (stdlib unittest)
├── requirements.txt        # runtime dependencies (radar only)
├── requirements-dev.txt    # + pytest, for development
├── requirements-live.txt   # + solders, only needed once wiring up real signing
└── .env.example            # documents every supported env var (no secrets)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # optional -- defaults work with no .env at all
```

No API key is required. DexScreener's endpoints used here are public.
`.env` is git-ignored -- if this project ever needs a real secret (an
exchange API key, a wallet key, etc.), put it in `.env` and reference it
from `src/config.py`; never commit it or hard-code it in source.

## Running the radar

Run it as a module from the project root, so the `src` package resolves
correctly:

```bash
python -m src.radar
```

This prints progress/log lines (level controlled by `LOG_LEVEL` in `.env`,
default `INFO`) followed by a ranked results table, for example:

```
=== FINAL RANKED RESULTS ===
[PASS]   BONK       | FINAL= 78/100 (base=80, momentum=75) | EARLY    | age=    8.2m | liq=$ 42,000 | vol24h=$ 210,000 | buys=800 sells=210 | trend=STRONG
[REJECT] SCAM       | FINAL=  5/100 (base=5, momentum=0)    | LATE     | age=  512.3m | liq=$  1,200 | vol24h=$     300 | buys=2 sells=40    | trend=WEAK

Pairs evaluated: 2 | Pairs passing first filter: 1
```

Each run also appends a snapshot per token to `data/snapshots.json`, which
is what lets `observation.py` compute a trend on the *next* run.

> **Network note:** DexScreener's API is unreachable from the sandboxed
> environments this project was built in (both the cloud session and its
> desktop-bridge shell have restricted egress), so most of this was
> validated with mocked data (see Testing) plus real runs confirmed
> working end-to-end on the machine actually running the bot -- that is
> where `python -m src.radar` should always be run from.

### Continuous mode

```bash
python -m src.radar --loop                       # run forever, every RADAR_LOOP_INTERVAL_SECONDS (default 60s)
python -m src.radar --loop --interval 60          # override the interval for this run
python -m src.radar --loop --max-iterations 5     # stop after 5 cycles (mainly for a bounded test/demo)
```

Stop an unbounded `--loop` run with Ctrl+C -- it exits cleanly after the
current cycle. A single cycle raising an unexpected error is logged and
the loop continues at the next scheduled interval rather than dying;
network failures were already handled gracefully before this (see
"Error handling notes" below), continuous mode just means the process
keeps going afterward instead of exiting.

The first few cycles after a fresh `data/snapshots.json` will show mostly
`INSUFFICIENT_DATA` (each token still only has one snapshot). From the
second time a token is seen onward -- either because it's still in
DexScreener's "latest" feed or because the watchlist pulled it back in --
`trend` becomes a real value.

## Testing

Tests use only the standard library (`unittest`), so there is nothing
extra to install:

```bash
python -m unittest discover -s tests -t . -v
```

If `pytest` is available (`pip install -r requirements-dev.txt`), it can
run the same tests with nicer output:

```bash
pytest
```

112 tests, all passing. Coverage: `stage`, `momentum`, `scoring` (pure
functions, including malformed/null-field inputs), `snapshot`
(save/load/trim/corrupt-file recovery, plus `known_addresses()`
watchlist ordering/limit), `observation` (trend logic, mocked snapshot
history), `dex_client` (batching, retries, failure handling, all with
`requests.get` mocked -- no real network calls), `radar` (end-to-end
`run_radar()` against fixture pair data, the watchlist keeping a token
in rotation across two cycles until it gets a real trend, `--loop`'s
iteration/interval/error-recovery/Ctrl+C behavior, and CLI parsing),
`paper_portfolio` + `paper_trader` (a full simulated buy -> take-profit
sell and buy -> stop-loss sell cycle with correct P&L, honeypot/low-score
rejection, and a regression test that paper_trader never imports
`wallet` or the kill switch), plus the live-trading safety layer:
`kill_switch` (every gate, engage/release), `portfolio` (position sizing
caps, stop-loss/take-profit, daily-loss cap, corrupt-state recovery),
`risk` (every rejection reason), `jupiter_client` (round-trip/honeypot
logic, mocked), `live_trader` (entry/exit decisions, including a
regression test that the module never imports `wallet`), and `wallet`
(seed-phrase detection, the `EXECUTION_ENABLED_IN_CODE` gate,
`connection_test()` degrading gracefully without network) -- all without
touching the network or a real wallet.

## Error handling notes

- Every network call goes through `dex_client`, which retries transient
  failures with backoff and gives up cleanly (logs and returns an empty
  result) rather than raising -- one bad request cannot crash a run.
- `evaluate_pair()` in `radar.py` wraps each individual pair in a
  try/except, so one malformed entry from the API is logged and skipped
  instead of aborting the whole batch.
- `utils.safe_get()` is used everywhere a nested field (e.g.
  `pair["liquidity"]["usd"]`) is read, because DexScreener sometimes sends
  an explicit `null` for a field instead of omitting it, which would
  otherwise crash a plain `.get(x, {}).get(y)` chain.
- `snapshot.py` recovers from a missing or corrupted `data/snapshots.json`
  by starting fresh instead of crashing, and logs the problem.

## Paper trading (simulated -- no wallet, no real funds, ever)

`src/paper_trader.py` runs the same *shape* of entry/exit pipeline the
live layer below uses (score -> trend -> liquidity/volume/age screening
-> the Jupiter sellability check -> sizing -> stop-loss/take-profit),
against fully simulated positions -- but with its own, deliberately more
permissive `PAPER_*` thresholds (`PAPER_MIN_SCORE=45` vs. live's
`MIN_LIVE_SCORE=80`; trend accepts `NEUTRAL` too, not just
`STRONG`/`RISING`; up to `PAPER_MAX_OPEN_POSITIONS=3` at once, not one).
`MIN_LIVE_SCORE` alone is close to the scoring formula's ceiling and was
found to reject essentially every real-world candidate, every cycle,
indefinitely -- see `src/config.py`'s comments above `PAPER_MIN_SCORE`
for the full reasoning and the real sample it's based on. The honeypot/
sellability check and the liquidity/volume/age *floors themselves* are
never weakened -- only which side of "reasonable vs. perfect" paper
trading is allowed to act on. It is structurally incapable of placing a
real order: it never imports `src/wallet.py` or `src/kill_switch.py` at
all (enforced by a regression test), and its state lives in
`data/paper_positions.json` / `data/paper_trade_log.jsonl` -- files the
live layer never reads or writes.

### Tuning the strategy from real results, not one or two trades

`scripts/backtest_paper_strategy.py` replays entry/exit rules against
every token this radar has ever recorded a snapshot for
(`data/snapshots.json`), using the exact same `src/scoring.py`,
`src/observation.py` and risk logic the live radar uses -- not a
hand-copied approximation -- so a candidate rule change can be compared
against the currently-deployed rules on real historical market data
before ever being adopted:

```bash
python -m scripts.backtest_paper_strategy
```

It is read-only: it never touches `data/paper_positions.json` or
`data/paper_trade_log.jsonl`. This is how the current defaults were
chosen: the first live paper-trading session (2026-09-03) closed 3/3
trades at a loss (-$6.28); the backtest replayed the same rules against
161 tracked tokens/~7000 snapshots and confirmed it wasn't a fluke --
20 trades, 20% win rate, -$21.99 total. Both real losers, and the
sweep's worst-performing region generally, shared the same pattern:
entries taken right at the age floor of that time (5 minutes, then
`MIN_LIVE_PAIR_AGE_MINUTES`'s value) were disproportionately early-life
pump-then-dump losers. Raising `PAPER_MIN_PAIR_AGE_MINUTES` to 15,
tightening `PAPER_TAKE_PROFIT_PCT` to 25 (from 50), and adding a
liquidity-drawdown guard (`PAPER_MAX_LIQUIDITY_DRAWDOWN_PCT`) and a
stop-loss cooldown (`PAPER_STOP_LOSS_COOLDOWN_MINUTES` -- a token isn't
re-bought right after its own stop-loss just fired) turned the same
replay into 7 trades, 85.7% win rate, +$8.12 -- only then was it
deployed. `PAPER_STOP_LOSS_PCT` itself was *not* the problem (it already
outperformed tighter alternatives in the sweep), so it is unchanged.

Every open/closed position also now records `entry_score`, `entry_
trend`, `entry_age_minutes`, `discovery_to_entry_seconds` (how long
between the radar first seeing the token and buying it) and the full
entry/exit reason text -- shown on the dashboard, and there for the next
round of this same replay-and-compare process.

```bash
python -m src.radar --paper                 # one cycle, paper decisions only
python -m src.radar --loop --paper           # continuous: radar + paper trading together
```

Each cycle: every open paper position is checked against the current
price for a stop-loss/take-profit/max-holding-time exit first; then each
qualifying candidate (highest score first, up to `PAPER_MAX_OPEN_
POSITIONS` and room under the daily-loss/capital-deployment caps) is
"bought" with simulated funds sized the same way live sizing would be
(`MAX_TRADE_USD`, capped by `MAX_CAPITAL_DEPLOYMENT_PCT` of
`TOTAL_CAPITAL_USD`) -- so one cycle can open several positions at once,
not just one, and a token already held is never bought again while its
position is still open. Every decision -- BUY, SKIP (with the exact
reason), SELL -- is appended to `data/paper_trade_log.jsonl`, tagged
`"mode": "PAPER"`.

A full buy-then-sell cycle (score/trend/risk screening passes -> position
opens with correct stop-loss/take-profit prices sized correctly ->
price crosses take-profit or stop-loss -> position closes with the
right realized P&L -> the slot frees up) is exercised end-to-end in
`tests/test_paper_trader.py` with synthetic price data, since waiting for
a real token to actually hit either level can take anywhere from minutes
to days. Run `python -m src.radar --loop --paper` for a while against
real market data to build up a realistic paper track record before ever
considering live trading.

To start a fresh paper run: `python -c "from src.paper_portfolio import reset_paper_state; reset_paper_state()"`
(only ever touches `data/paper_positions.json`).

## X (Twitter) social intelligence -- optional, additive, off by default

`src/x_intelligence.py` is the radar's only import point for six small
modules that turn X posts into an extra, *never-required* scoring
signal:

| Module | Job |
|---|---|
| `x_client.py` | Talks to X API v2's recent-search endpoint. Zero network calls, zero cost, unless `X_BEARER_TOKEN` is set. Retries with backoff, honors 429 rate-limit reset headers (capped wait), and enforces a hard daily read budget (`X_MAX_READS_PER_DAY`, `data/x_usage.json`). |
| `x_signal_engine.py` | Extracts cashtags/hashtags/meme-context tickers from post text, clusters them into per-entity trend state (`data/x_signals.json`), computes independent-mention velocity, and flags spam-shaped text. |
| `x_account_reputation.py` | Learns a per-account weight (`data/x_account_reputation.json`) purely from outcomes -- not follower count -- via EMA smoothing so recent results matter more than old ones. |
| `x_correlation.py` | Fuzzy-matches an entity to a real radar-known token by symbol (stdlib `difflib`), and flags a near-identical symbol as a possible clone when an older/more-liquid "original" exists. |
| `x_intelligence.py` | Orchestrates the above once per radar cycle (rate-limited to `X_POLL_INTERVAL_SECONDS`, independent of the radar's own faster loop) and is the hard resilience boundary: every function here catches everything and degrades to "no signal" rather than ever raising into `radar.py`. |

**Why off by default:** as of 2026, X's API has no free tier for a new
project -- reads are billed pay-per-use (~$0.005/post) or via a legacy
$200+/month subscription. Nothing in this project has ever set
`X_BEARER_TOKEN` or spent anything against a real X account. Leave it
empty and every X-related function above no-ops immediately.

**How it plugs into scoring, never as a gate:** `radar.run_radar()`
correlates each cycle's results against active X trend clusters
(`_apply_x_social_signals()`) and adds a bounded points bonus
(`X_SCORE_MAX_BONUS`, default 10) on top of the market-data score --
`src/scoring.calculate_score()`'s new `social_bonus` parameter defaults
to 0, so a token with no X signal (or X not configured at all) scores
exactly as it always has. A signal flagged as a possible clone
contributes zero bonus. Every result carries `x_trend_detected`,
`x_entity`, `social_velocity`, `source_quality`, `independent_mentions`,
`social_confidence`, `possible_clone` -- shown on the dashboard's
opportunity cards, and recorded into `data/opportunity_watchlist.json`'s
history the same way the existing momentum signals are.

**Closing the learning loop:** when `src.paper_trader` opens a position
whose token correlated to an X entity, the position remembers that
entity; when the position closes, every account that contributed a
mention gets `x_account_reputation.record_outcome()`'d with whether the
trade won or lost -- see `scripts/backtest_x_signals.py` for a
synthetic-scenario replay proving that mechanism actually rewards
consistently-useful accounts over noisy/spam ones (no real historical X
data exists yet to replay for real; this validates the *mechanism*, not
a specific account's track record).

```bash
python -m scripts.backtest_x_signals   # synthetic reputation-learning replay
```

### Connecting your own X account (optional)

Everything above works with zero X credentials. To actually turn it on
(2026-09-03 pricing, checked live -- reconfirm on
[developer.x.com](https://developer.x.com) before relying on this, X
changes these terms periodically):

1. **Sign in at [developer.x.com](https://developer.x.com)** with your
   own X account (a phone-verified account is typically required for
   developer access) and complete the one-time developer profile setup
   if prompted. *This step needs you personally -- login, any 2FA
   prompt, and the developer terms acceptance are all things only you
   can do.*
2. **Create a Project, then an App inside it** (e.g. project "meme
   radar", app "x-signal-client"). The **Free** project tier costs
   nothing and needs no payment method -- but as of 2026 its read
   limits are extremely tight (effectively near-unusable for
   continuous polling; X's paid **Basic** tier, ~$200/month, is what
   real continuous access requires). Start on Free to prove the
   connection works end to end; decide separately, later, whether the
   volume is worth paying for -- this project never assumes that
   decision for you.
3. **App → "Keys and Tokens" tab → generate the Bearer Token** (App-only
   auth -- that's all this project uses; it never posts, follows, or
   writes anything).
4. **Paste it into `.env`** (project root, already git-ignored) on the
   `X_BEARER_TOKEN=` line. Never paste it into a chat, an issue, or
   anywhere else.
5. Run the one script in this project that deliberately makes a real
   network call, to confirm the token actually works:
   ```bash
   python -m scripts.test_x_connection
   ```
   It reports success/failure, shows a couple of real posts fetched,
   and confirms they were fed through `x_signal_engine` -- all without
   printing your token back anywhere.
6. Restart `python -m webapp.app` (or the radar process directly) --
   `X_BEARER_TOKEN` is read once at process start, so an already-running
   process needs a restart to pick up a freshly-added token.

## Live trading (disabled by default -- read this before changing anything)

A safety layer for real execution has been built. `src/wallet.py`'s
`build_and_send_swap()` is **fully implemented** (build the swap via
Jupiter, sign it locally with your key, submit it, poll for on-chain
confirmation), and `live_trader.py`'s automatic BUY/SELL cycle is now
**wired to actually call it** through `_attempt_real_buy()` /
`_attempt_real_sell()`. None of this has ever run end to end -- the
environment that wrote it has no `solders` package installed and no
network path to Jupiter or Solana RPC -- and it is still hard-refused by
`EXECUTION_ENABLED_IN_CODE = False` in `src/wallet.py`, independently of
`LIVE_TRADING`/`CONFIRM_LIVE_TRADING`. As long as that one source-level
constant stays `False` -- true of every configuration this project has
ever run with -- a BUY/SELL decision behaves exactly as before this
wiring existed: it is decided and logged, and local bookkeeping
(`data/positions.json`) is only ever updated *after* a real swap is
confirmed on-chain, so it can never claim a position exists that
doesn't. The remaining step is the same as it always was: a human, on
their own machine, testing a real trade for a trivial amount before ever
flipping that constant.

### What's implemented

| Module | Purpose |
|---|---|
| `kill_switch.py` | The single gate every live path must pass: `LIVE_TRADING=true` **and** `CONFIRM_LIVE_TRADING` matching an exact phrase **and** no `data/STOP_TRADING` file present. Re-checked fresh before every decision. |
| `risk.py` | Rejects a candidate below stricter live-only thresholds (liquidity, volume, pair-age window) or that fails the sellability check. |
| `jupiter_client.py` | Read-only Jupiter quotes; `round_trip_check()` simulates a buy-then-sell to catch tokens that can be bought but not sold ("honeypots") *before* ever risking funds. |
| `portfolio.py` | Position sizing hard-capped at `MAX_TRADE_USD`, never deploys more than `MAX_CAPITAL_DEPLOYMENT_PCT` of `TOTAL_CAPITAL_USD`, one open position at a time by default, stop-loss/take-profit price tracking, daily realized-loss cap. |
| `live_trader.py` | Combines the above into entry/exit decisions, then **attempts** real execution via `wallet.build_and_send_swap()`. Every decision (and every execution attempt's outcome) is logged. `open_position()`/`close_position()` are only ever called after a real swap is confirmed on-chain. |
| `trade_logger.py` | Appends every decision (BUY/SKIP/SELL/BLOCKED/ERROR/UNCONFIRMED + reason) to `data/trade_log.jsonl`. |
| `wallet.py` | Loads a keypair from `SOLANA_PRIVATE_KEY` (env-only), refuses anything that looks like a pasted seed phrase, and provides a **read-only** `connection_test()` (RPC health + balance, needs only your public address) and `get_spl_token_balance_raw()` (reads the actual on-chain balance of a token, so a real sell always sells exactly what is held, not a locally-tracked estimate). `build_and_send_swap()` is fully implemented (build via Jupiter → sign locally → submit → poll for confirmation) but untested end to end, and is gated by a source-level constant (`EXECUTION_ENABLED_IN_CODE = False`) that no env variable can override. |

### Why nothing has run live yet

1. **This environment cannot reach Solana or Jupiter.** `quote-api.jup.ag`,
   `api.mainnet-beta.solana.com` and `price.jup.ag` are all unreachable
   from the sandbox this was built in (same restriction that blocked
   DexScreener earlier), and it also has no `solders` package installed.
   The safety logic and `build_and_send_swap()`'s own orchestration are
   fully unit-tested with mocked network/signing calls (86 tests across
   the live-trading safety layer), but the "connection test", any real
   quote, and a real signed transaction have to be run from a machine
   with normal internet access -- yours, not this one.
   (Update, 2026-09-03, run from an actual machine: `quote-api.jup.ag`
   itself turned out to have been deprecated by Jupiter on 2025-10-01 and
   no longer resolves in DNS anywhere, sandbox or not -- `src/config.py`'s
   `JUPITER_QUOTE_URL`/`JUPITER_SWAP_URL` now point at `lite-api.jup.ag`,
   Jupiter's current free tier, confirmed reachable and returning real
   quotes. Solana RPC/`solders` still untested here.)
2. **No private key has been provided, and none should be pasted here.**
   A seed phrase or private key typed into any chat -- this one included
   -- should be treated as compromised the moment it's typed. Real
   execution has to run somewhere you control directly: your own machine,
   with your own local `.env` file that never leaves it.
3. **You asked to see the full plan before anything is enabled**, and
   that plan follows below.

### The plan, as requested

- **Wallet / connection method:** a standard Solana keypair, provided
  only via `SOLANA_PRIVATE_KEY` in a **local** `.env` file (never
  committed, never pasted into chat). `SOLANA_WALLET_PUBLIC_KEY` (your
  address, not secret) can be set on its own first to run
  `wallet.connection_test()` -- RPC reachability + balance -- with no
  private key involved at all. Recommend starting with a **new, dedicated
  wallet holding only the ~$24** you mentioned, not a wallet with other
  funds in it.
- **First trade size:** `MAX_TRADE_USD` defaults to **$5** (about 20% of
  $24), and `MAX_CAPITAL_DEPLOYMENT_PCT=80%` means at most ~$19.20 is ever
  deployed in total, always leaving a reserve for Solana network fees.
  Both are adjustable in `.env`.
- **Entry conditions (all required):** live score ≥ `MIN_LIVE_SCORE`
  (80/100, stricter than the general radar) · trend is `STRONG` or
  `RISING` (never `WEAK`/`NEUTRAL`/unknown) · liquidity ≥ $15,000 and 24h
  volume ≥ $50,000 (stricter than the discovery filter) · pair age between
  5 and 180 minutes (skips the highest-risk first few minutes and pairs
  past their momentum window) · a Jupiter round-trip quote confirms the
  token can actually be sold back, with round-trip cost under 20% · room
  under the position-count and daily-loss caps.
- **Exit conditions:** stop-loss at **-25%** from entry, take-profit at
  **+50%** from entry (`STOP_LOSS_PCT` / `TAKE_PROFIT_PCT`, both
  adjustable). Checked every cycle against the open position.
- **Immediate stop:** `touch data/STOP_TRADING` at any time -- checked
  fresh before every single decision, no restart needed. Removing that
  file (or just setting `LIVE_TRADING=false`) is required to resume.
  `kill_switch.engage_kill_switch()` / `release_kill_switch()` do the same
  programmatically.

### What real execution still needs, before it can run even once

The code path is complete end to end (`radar` → `live_trader` decision →
`wallet.build_and_send_swap()` → confirmed on-chain → `portfolio`
bookkeeping). What's left is entirely verification and a deliberate
human decision, not more code:

1. `pip install -r requirements-live.txt` (adds `solders`; not needed for
   anything else in this project) on a machine with real internet access
   -- yours, not the sandbox this was built in.
2. Set `SOLANA_WALLET_PUBLIC_KEY` in a **local** `.env` and run
   `wallet.connection_test()` for real -- confirms the RPC endpoint and
   wallet resolve correctly, with no private key involved at all.
3. **Test `build_and_send_swap()` yourself, against a throwaway wallet
   holding a trivial amount (well under $1) -- never the $24 wallet, and
   never through an AI assistant.** It has been implemented and unit
   tested with mocked network/signing calls, and `live_trader.py`'s call
   into it has its own orchestration tests (81 tests across the
   live-trading + execution-wiring layer total), but every real on-chain
   behavior -- does the transaction actually land, is the confirmation
   logic right, is the fee reasonable, does `get_spl_token_balance_raw()`
   read the right amount back -- is unverified until you watch a full
   buy-then-sell round trip succeed on a block explorer (e.g. solscan.io)
   with your own eyes, repeatedly, before trusting it further. (86 tests
   across `wallet.py`, `live_trader.py`, `jupiter_client.py`,
   `kill_switch.py`, `portfolio.py`, and `risk.py` combined.)
4. Only after 1-3, and only by deliberately editing `src/wallet.py`'s
   `EXECUTION_ENABLED_IN_CODE` to `True` (a source change, not an env
   setting) plus setting `LIVE_TRADING=true` and the exact
   `CONFIRM_LIVE_TRADING` phrase in a local `.env` -- all three at once
   -- would a real order ever be sent.

None of that has been done. No real trade has been placed, and none will
be without those explicit steps -- each of which requires you, personally,
on your own machine, not an automated or unattended process.

## What's next

- Run `python -m src.radar --loop --paper` for a real stretch of time to
  build an actual paper track record before considering live trading.
- Test the real execution path yourself against a throwaway wallet (see
  above) -- the one remaining piece standing between this and real
  execution is verification, not more wiring.
- `live_trader.run_live_cycle()` still expects a `{token_address: price}`
  map for exit checks to be supplied by the caller (paper_trader.py
  already derives this from each cycle's radar results -- the live path
  should do the same once it's ever wired up for real).
- CI to run the test suite automatically on push.
