# US Stocks -- Live Trading Gate & Runbook

**This is a human-readable runbook, written once and maintained by hand.**
It is separate from `STOCKS_LIVE_READINESS_REPORT.md`, which is
auto-regenerated every time `python -m scripts.research_stocks_strategies`
runs and reflects the latest backtest numbers only. This file answers a
different question: *what, concretely, has to happen -- in code and in a
human decision -- before this system could ever place a real order?*

Written: 2026-09-04. Baseline: git tag `stocks-stable-pre-live-v1`
(commit `c8d93d7`), plus the strategy-registry re-confirmation recorded
2026-09-03T23:45:37Z. Updated the same day once the gated live-execution
layer described below was actually built and unit-tested (mocks only --
see Section 2).

---

## 1. Where the system stands today

- **Active strategy:** `breakout`, chosen via `src/stocks/strategy_registry.py`.
  Validated LIVE_CANDIDATE on a 10-year/47-symbol/5-fold walk-forward run
  with out-of-sample holdout: OOS n=286, win%=48.3, profit factor=1.66,
  expectancy=1.13%/trade, fold_stability=0.60 (at the pass threshold),
  return-to-drawdown ratio=5.84. `momentum` is the stronger/more stable
  alternative on paper (fold_stability=1.0, larger OOS sample) but a lower
  per-trade edge (OOS PF=1.29, expectancy=0.64%); kept as the recorded
  runner-up rather than switched to.
- **Paper trading:** running continuously, gated correctly by market hours,
  self-monitoring via `src/stocks/health.py`, auto-recovering per the
  three-layer resilience logic in `engine.py::run_forever()`. Completely
  unaffected by anything below -- no file described in this document is
  imported by `src/stocks/engine.py` or by `webapp/app.py`.
- **Real-money execution: a fully implemented, fully gated, and so far
  never-exercised-for-real code path now exists** in
  `src/stocks/live_broker.py` (+ `live_trader.py`, `live_ledger.py`,
  `live_risk.py`, `live_logger.py`, `kill_switch.py`) -- see Section 2 for
  what it does and Section 3 for the three gates that keep it closed.
  `src/stocks/alpaca_client.py` (the module the running paper-trading loop
  actually uses) is unchanged and still has no path to `api.alpaca.markets`
  at all.

## 2. What was built, and how it stays safe with zero risk of accidental use

Six new files, none of them imported by anything that actually runs
continuously on this machine:

| File | Role |
|---|---|
| `src/stocks/kill_switch.py` | Layers 2+3 of the gate (below) + an immediate, file-based emergency stop -- mirrors `src/kill_switch.py` exactly. |
| `src/stocks/live_broker.py` | The only module that can reach `api.alpaca.markets`. Layer 1 of the gate lives here. Raw order submission/polling/cancel, with duplicate-order and ambiguous-outcome handling (Section 5). |
| `src/stocks/live_risk.py` | Live-only position sizing and entry gating (daily loss cap, circuit breaker, overtrading guard, max open positions) against a small, separate live capital baseline -- pure functions, no I/O. |
| `src/stocks/live_ledger.py` | Local record of real positions (`data/stocks/live_positions.json`), written only after a real fill is confirmed -- never on intent. |
| `src/stocks/live_logger.py` | Full audit log of every live decision/attempt/outcome (`data/stocks/live_trade_log.jsonl`), separate from the paper log. |
| `src/stocks/live_trader.py` | Orchestrates all of the above: `evaluate_live_entry`/`attempt_live_buy`, `evaluate_live_exit`/`attempt_live_sell`, `emergency_stop()`. |

**The single most important safety property of this whole layer: it is
never imported by `src/stocks/engine.py` (the continuously-running paper
loop) or by `webapp/app.py`** -- confirmed by grep, and it is what
`tests/test_webapp.py`/`tests/stocks/test_engine.py`'s existing isolation
checks would catch if that ever changed. This mirrors the crypto side's
own precedent exactly: `src/live_trader.py` has been fully implemented
there for a while and is *also* never imported by `src/radar.py` or the
running webapp process (see `src/cli.py`'s docstring) -- the capability
exists and is tested, but nothing running invokes it. The only way any of
this code ever runs is a human deliberately importing `src.stocks.live_trader`
from a separate script/shell and calling it -- and even then, every one of
the three gates below still has to be open.

Tested exclusively against mocks (`unittest.mock` patches on
`requests.get/post/delete` and on each other's functions) -- **82 new
tests, zero of which touch a real network socket**, covering: every gate
closed (the default, real, unpatched state), duplicate-order protection
(local ledger check + a fresh live open-orders check), balance/buying-power
verification, position sizing against a real buying-power figure, the
daily-loss cap and drawdown circuit breaker, order rejection, an
ambiguous (timed-out/disconnected) submission never being silently
retried, fill polling and its own timeout, and the emergency stop. See
`tests/stocks/test_kill_switch.py`, `test_live_risk.py`,
`test_live_ledger.py`, `test_live_broker.py`, `test_live_trader.py`.

## 3. The three-layer gate (built, closed, verified closed)

1. **`STOCKS_EXECUTION_ENABLED_IN_CODE`** -- a module-level constant in
   `src/stocks/live_broker.py`, currently `False`, with **no environment
   override at all**. Flipping it requires hand-editing and redeploying
   that source line -- a deliberate, reviewed code change, never a config
   or `.env` change. Checked first, inside `live_broker.py` itself,
   before any other function in that module does anything network-related
   -- verified by `TestLayer1GateAlwaysCheckedFirst` in
   `tests/stocks/test_live_broker.py`.
2. **`STOCKS_LIVE_TRADING`** -- `src/stocks/config.py`, env-backed,
   defaults `False`. Mirrors the crypto side's `LIVE_TRADING`.
3. **`STOCKS_CONFIRM_LIVE_TRADING`** -- must exactly equal
   `STOCKS_REQUIRED_CONFIRM_PHRASE = "I_UNDERSTAND_AND_APPROVE_STOCKS_LIVE_TRADING"`.
   A single accidental `STOCKS_LIVE_TRADING=true` in a copied `.env` is
   not enough on its own.

Layers 2+3 together are `src.stocks.kill_switch.trading_allowed()`,
re-checked fresh before every single decision (not just at startup) --
plus an independent, immediate file-based kill switch
(`data/stocks/STOP_LIVE_TRADING`) that halts everything the moment the
file exists, no restart required. `src.stocks.live_trader.emergency_stop()`
engages that kill switch and (best-effort) cancels every open real order
in one call.

**Right now, in this repository, all three layers are closed** -- Layer 1
is `False` in source, Layer 2 defaults `False` and nothing sets it,
Layer 3's env var is empty. `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_API_SECRET`
(a separate key pair from the paper ones) are also unset, which would
independently block everything even if the three gates above were open.

## 4. What "starting Live with a very small amount" actually requires now

The gated code exists; what's left is entirely account setup and your
explicit, deliberate go-ahead -- there is no more code-writing step
blocking this:

1. **A live (not paper) Alpaca account**, funded with an amount you are
   fully prepared to lose in full -- API keys for it go into
   `ALPACA_LIVE_API_KEY`/`ALPACA_LIVE_API_SECRET` in a local, git-ignored
   `.env`.
2. **Review the live pilot risk limits** in `src/stocks/config.py`
   (`STOCKS_LIVE_STARTING_CAPITAL_USD=200`, `STOCKS_LIVE_MAX_POSITION_USD=25`,
   `STOCKS_LIVE_MAX_OPEN_POSITIONS=1`, `STOCKS_LIVE_MAX_TRADES_PER_DAY=2`,
   `STOCKS_LIVE_MAX_DAILY_LOSS_PCT=3`, `STOCKS_LIVE_MAX_DRAWDOWN_PCT=10`) --
   adjust the env vars if you want them smaller/larger before ever going
   live; nothing about them affects paper trading.
3. **You set `STOCKS_LIVE_TRADING=true` and `STOCKS_CONFIRM_LIVE_TRADING=I_UNDERSTAND_AND_APPROVE_STOCKS_LIVE_TRADING`**
   in your local `.env` (Layers 2+3) -- I will not do this for you.
4. **You hand-edit `STOCKS_EXECUTION_ENABLED_IN_CODE = True` in
   `src/stocks/live_broker.py`** (Layer 1) -- the one step that is,
   deliberately, not an env var at all. This is the actual moment real
   execution becomes possible, and it should be a reviewed code change,
   ideally with a fresh `git diff` read line by line right before it.
5. **Run one real, trivial trade yourself first** (a fraction of the pilot
   size) and verify it in the Alpaca live dashboard before trusting this
   to the continuous loop, if you ever choose to wire it in at all --
   which, per Section 2, it currently is not.
6. **Monitor closely** via `data/stocks/live_trade_log.jsonl` and
   `data/stocks/live_positions.json` (or a small dashboard addition, not
   built yet) before any size increase.
7. If anything looks wrong at any point: call
   `src.stocks.live_trader.emergency_stop()`, or simply create
   `data/stocks/STOP_LIVE_TRADING` by hand -- takes effect immediately,
   no restart.

## 5. How specific requested safety properties are actually implemented

- **No real order during tests:** Layer 1 (`STOCKS_EXECUTION_ENABLED_IN_CODE`)
  is `False` in source and no test flips the real module-level constant
  outside a scoped `mock.patch.object` (verified: `git grep` for the
  constant shows it only ever set `True` inside `with mock.patch...`
  blocks) -- structurally, not by convention alone.
- **Duplicate-order protection:** two independent checks before every
  submission -- the local ledger (`live_ledger.has_open_position`) AND a
  fresh read of Alpaca's own open orders for that symbol
  (`live_broker.list_live_open_orders`), so a stale/lost local write can't
  cause a second real order.
- **Balance/buying-power check:** `live_broker.get_live_account()` is read
  fresh before every entry; sizing never exceeds buying power minus a
  configurable safety buffer (`STOCKS_LIVE_MIN_BUYING_POWER_BUFFER_USD`).
- **Position sizing:** `live_risk.compute_live_position_size_usd()` --
  capped by `STOCKS_LIVE_MAX_POSITION_USD`, the live deployment-pct cap,
  and real buying power, whichever is smallest.
- **Stop-loss/take-profit/trailing:** reuses `src.stocks.risk_engine`'s
  exact, already-deeply-validated ATR functions unchanged -- not a second
  implementation that could drift from what was backtested.
- **Max daily loss / circuit breaker:** `live_risk.can_open_new_live_position()`
  -- a daily realized-loss cap and a peak-equity drawdown circuit breaker,
  against the small live capital baseline, independent of paper trading's.
- **Full logging:** every decision (BLOCKED/SKIP/BUY/SELL/ERROR/UNCONFIRMED)
  goes through `live_logger.log_decision()` into its own JSONL file, never
  only the successes.
- **Rejection/timeout/disconnection handling:** `live_broker.submit_live_order()`
  distinguishes a definite rejection (`LiveOrderRejected`, a 4xx response --
  safe to treat as "did not happen") from a genuinely ambiguous network
  failure (`LiveOrderAmbiguous`, e.g. a timeout before any response) --
  the latter is **never silently retried** (a retried order risks a double
  fill); the caller is required to reconcile via
  `list_live_open_orders()`/`get_live_positions()` before doing anything
  else with that symbol. `poll_order_fill()` handles a fill that takes a
  while, or never resolves within its timeout, the same careful way
  `src.wallet._poll_confirmation()` does on the crypto side.
- **Kill switch / emergency stop:** `src.stocks.kill_switch` (file-based,
  immediate, no restart) plus `live_trader.emergency_stop()` (kill switch
  + best-effort cancel of every open real order).

## 6. Remaining risks (technical, not financial-advice)

- This code has never been run against Alpaca's real live endpoint --
  only against mocks. The general shapes (order lifecycle, status
  strings, error codes) follow Alpaca's public API docs, but a first real
  run should be treated as the actual integration test, at trivial size.
- `fold_stability` for breakout sits exactly at the 0.60 pass threshold,
  not comfortably above it -- worth re-checking after a few more months of
  data before increasing size past the initial pilot.
- This layer is not wired into the continuous loop or the dashboard by
  design (Section 2) -- using it today means writing a small one-off
  script that imports `src.stocks.live_trader` directly; there is no
  "Go Live" button anywhere, deliberately.

## 7. Bottom line

`STOCKS_LIVE_TRADING` defaults `False`, `STOCKS_EXECUTION_ENABLED_IN_CODE`
is `False` in source, `STOCKS_CONFIRM_LIVE_TRADING` is unset, live Alpaca
credentials are unset, and none of the six files above are imported by
anything that runs continuously. No real order has been placed, and none
can be, until a human completes every step in Section 4.
