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
python -m src.radar --loop                       # run forever, every RADAR_LOOP_INTERVAL_SECONDS (default 300s)
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

`src/paper_trader.py` runs the *exact same* entry/exit rules the live
layer below uses (`MIN_LIVE_SCORE`, trend must be `STRONG`/`RISING`,
liquidity/volume/age screening, the Jupiter sellability check, stop-loss
`-25%`/take-profit `+50%`, one position at a time, the daily-loss cap)
against a fully simulated position. It is structurally incapable of
placing a real order: it never imports `src/wallet.py` or
`src/kill_switch.py` at all (enforced by a regression test), and its
state lives in `data/paper_positions.json` / `data/paper_trade_log.jsonl`
-- files the live layer never reads or writes.

```bash
python -m src.radar --paper                 # one cycle, paper decisions only
python -m src.radar --loop --paper           # continuous: radar + paper trading together
```

Each cycle: any open paper position is checked against the current price
for a stop-loss/take-profit exit first; then the highest-scoring
qualifying candidate (if any, and if there's room under the position/
daily-loss caps) is "bought" with simulated funds sized the same way live
sizing would be (`MAX_TRADE_USD`, capped by `MAX_CAPITAL_DEPLOYMENT_PCT`
of `TOTAL_CAPITAL_USD`). Every decision -- BUY, SKIP (with the exact
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

## Live trading (disabled by default -- read this before changing anything)

A safety layer for real execution has been built, and `src/wallet.py`'s
`build_and_send_swap()` is now **fully implemented** (build the swap via
Jupiter, sign it locally with your key, submit it, poll for on-chain
confirmation) -- but it has **never been run end to end**, because the
environment that wrote it has no `solders` package installed and no
network path to Jupiter or Solana RPC. It is still hard-refused by
`EXECUTION_ENABLED_IN_CODE = False` and by `LIVE_TRADING`/
`CONFIRM_LIVE_TRADING` regardless of that. `live_trader.py`'s automatic
BUY/SELL cycle still only *decides and logs* -- it does not call
`build_and_send_swap()` itself. That wiring, and the first real test,
are the one deliberate stopping point left (see below): they need to
happen on a machine with real network access, run by you, not
automatically.

### What's implemented

| Module | Purpose |
|---|---|
| `kill_switch.py` | The single gate every live path must pass: `LIVE_TRADING=true` **and** `CONFIRM_LIVE_TRADING` matching an exact phrase **and** no `data/STOP_TRADING` file present. Re-checked fresh before every decision. |
| `risk.py` | Rejects a candidate below stricter live-only thresholds (liquidity, volume, pair-age window) or that fails the sellability check. |
| `jupiter_client.py` | Read-only Jupiter quotes; `round_trip_check()` simulates a buy-then-sell to catch tokens that can be bought but not sold ("honeypots") *before* ever risking funds. |
| `portfolio.py` | Position sizing hard-capped at `MAX_TRADE_USD`, never deploys more than `MAX_CAPITAL_DEPLOYMENT_PCT` of `TOTAL_CAPITAL_USD`, one open position at a time by default, stop-loss/take-profit price tracking, daily realized-loss cap. |
| `live_trader.py` | Combines the above into entry/exit decisions. **Every decision is logged; none is executed.** |
| `trade_logger.py` | Appends every decision (BUY/SKIP/SELL/BLOCKED + reason) to `data/trade_log.jsonl`. |
| `wallet.py` | Loads a keypair from `SOLANA_PRIVATE_KEY` (env-only), refuses anything that looks like a pasted seed phrase, and provides a **read-only** `connection_test()` (RPC health + balance, needs only your public address). `build_and_send_swap()` is fully implemented (build via Jupiter → sign locally → submit → poll for confirmation) but untested end to end, and is additionally gated by a source-level constant (`EXECUTION_ENABLED_IN_CODE = False`) that no env variable can override. |

### Why nothing has run live yet

1. **This environment cannot reach Solana or Jupiter.** `quote-api.jup.ag`,
   `api.mainnet-beta.solana.com` and `price.jup.ag` are all unreachable
   from the sandbox this was built in (same restriction that blocked
   DexScreener earlier), and it also has no `solders` package installed.
   The safety logic and `build_and_send_swap()`'s own orchestration are
   fully unit-tested with mocked network/signing calls (67 tests across
   the live-trading safety layer), but the "connection test", any real
   quote, and a real signed transaction have to be run from a machine
   with normal internet access -- yours, not this one.
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

1. `pip install -r requirements-live.txt` (adds `solders`; not needed for
   anything else in this project) on a machine with real internet access
   -- yours, not the sandbox this was built in.
2. Set `SOLANA_WALLET_PUBLIC_KEY` in a **local** `.env` and run
   `wallet.connection_test()` for real -- confirms the RPC endpoint and
   wallet resolve correctly, with no private key involved at all.
3. **Test `build_and_send_swap()` yourself, against a throwaway wallet
   holding a trivial amount (well under $1) -- never the $24 wallet, and
   never through an AI assistant.** It has been implemented and unit
   tested with mocked network/signing calls (14 new tests), but every
   real on-chain behavior -- does the transaction actually land, is the
   confirmation logic right, is the fee reasonable -- is unverified until
   you watch one succeed on a block explorer (e.g. solscan.io) with your
   own eyes.
4. Wire the actual call: today `live_trader.run_live_cycle()` decides and
   logs a BUY/SELL but does not call `wallet.build_and_send_swap()` --
   that connection is deliberately not made yet, so a BUY/SELL decision
   currently just updates `data/positions.json`'s bookkeeping, exactly
   like paper trading. Add that call once step 3 has passed repeatedly.
5. Only after 1-4, and only by deliberately editing `src/wallet.py`'s
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
- Test `wallet.build_and_send_swap()` yourself against a throwaway wallet
  (see above), then wire it into `live_trader.run_live_cycle()` -- the two
  remaining pieces standing between this and real execution.
- `live_trader.run_live_cycle()` still expects a `{token_address: price}`
  map for exit checks to be supplied by the caller (paper_trader.py
  already derives this from each cycle's radar results -- the live path
  should do the same once it's ever wired up for real).
- CI to run the test suite automatically on push.
