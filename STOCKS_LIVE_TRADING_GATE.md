# US Stocks -- Live Trading Gate & Runbook

**This is a human-readable runbook, written once and maintained by hand.**
It is separate from `STOCKS_LIVE_READINESS_REPORT.md`, which is
auto-regenerated every time `python -m scripts.research_stocks_strategies`
runs and reflects the latest backtest numbers only. This file answers a
different question: *what, concretely, has to happen -- in code and in a
human decision -- before this system could ever place a real order?*

Written: 2026-09-04. Baseline: git tag `stocks-stable-pre-live-v1`
(commit `c8d93d7`), plus the strategy-registry re-confirmation recorded
2026-09-03T23:45:37Z.

---

## 1. Where the system stands today

- **Active strategy:** `breakout`, chosen via `src/stocks/strategy_registry.py`.
  Validated LIVE_CANDIDATE on a 10-year/47-symbol/5-fold walk-forward run
  with out-of-sample holdout: OOS n=286, win%=48.3, profit factor=1.66,
  expectancy=1.13%/trade, fold_stability=0.60 (at the pass threshold),
  return-to-drawdown ratio=5.84. `momentum` is the stronger/more stable
  alternative on paper (fold_stability=1.0, larger OOS sample) but a lower
  per-trade edge (OOS PF=1.29, expectancy=0.64%); kept as the recorded
  runner-up rather than switched to, per the standing instruction not to
  change strategy without a clear reason to.
- **Paper trading:** running continuously, gated correctly by market hours,
  self-monitoring via `src/stocks/health.py`, auto-recovering per the
  three-layer resilience logic in `engine.py::run_forever()`.
- **Real-money execution: structurally impossible today, not just
  flag-disabled.** `src/stocks/config.py` hardcodes
  `ALPACA_TRADING_BASE_URL = "https://paper-api.alpaca.markets"` as a
  Python string literal (not read from any environment variable), and
  `src/stocks/alpaca_client.py` has **no code path anywhere** that reaches
  `api.alpaca.markets` (the live endpoint). `STOCKS_LIVE_TRADING = False`
  is also hardcoded in `config.py` (line 62) and nothing in the codebase
  reads it as a live-order gate yet, because there is no live-order code
  to gate.
- **Crypto side, for comparison:** `src/wallet.py` / `src/live_trader.py`
  / `src/config.py` already implement real-money execution against a live
  endpoint, gated by three independent layers: `EXECUTION_ENABLED_IN_CODE`
  (a module constant, not env-configurable -- must be hand-edited in the
  source file), `LIVE_TRADING` (env var, default `False`), and
  `CONFIRM_LIVE_TRADING` (env var that must exactly equal the literal
  phrase `I_UNDERSTAND_AND_APPROVE_LIVE_TRADING`). All three currently
  still evaluate to "blocked", and none of this work touched them.

## 2. Why no live-order code was written in this pass

Turning the stocks side live requires *adding* a new code path (a real
order-submission function against Alpaca's live API), not just changing a
flag. Writing that function now -- even fully gated off -- would trade the
current, stronger safety property ("the capability to place a real stock
order does not exist in this codebase") for a weaker one ("the capability
exists but every layer says no"), which is exactly the crypto-side model.
That is a legitimate and correct engineering pattern, and it is what
Section 3 below specifies as a template -- but doing it as a byproduct of
a "prepare the gate, don't go live" request, without a separate, explicit
instruction to write real order-execution code, is the wrong moment for
it: every instruction across this whole project's live-trading requests
has been "prepare, validate, report -- do not activate." Building the gate
itself (this document, the checklist, the template) fully serves item 4
of the request ("prepare a gate that can never open automatically")
without adding a single new way for money to move. The actual
order-execution implementation is listed below as the explicit next step,
to be done as its own reviewable change when you ask for it.

## 3. The three-layer gate this system will use (template, not yet built)

When live-order execution is implemented for stocks, it will mirror the
crypto side's pattern in `src/stocks/`:

1. **`STOCKS_EXECUTION_ENABLED_IN_CODE`** -- a module-level constant in
   `src/stocks/alpaca_client.py`, default `False`, `# type: bool` with no
   environment override. Flipping it requires editing and re-deploying
   source code, not setting an env var -- so a leaked or mistyped `.env`
   can never enable it by itself.
2. **`STOCKS_LIVE_TRADING`** -- the existing `config.py` env-backed flag,
   default `False`. Promoted from "unused placeholder" to "real gate"
   only once step 1 exists.
3. **`STOCKS_CONFIRM_LIVE_TRADING`** -- a new env var that must exactly
   equal a required phrase (mirroring
   `REQUIRED_CONFIRM_PHRASE = "I_UNDERSTAND_AND_APPROVE_LIVE_TRADING"` on
   the crypto side), so a single stray `true` cannot activate real trading
   by accident.

Additionally, `ALPACA_TRADING_BASE_URL` would need to become
env-selectable (currently it cannot be, by design), with the live value
requiring the same three gates above to even be read.

All three gates would need to independently evaluate true before a single
real order could be submitted -- exactly as on the crypto side today.

## 4. What "starting Live with a very small amount" will actually require, in order

1. **You give explicit approval to implement the gated execution path**
   (this is a separate step from this document -- writing real
   order-submission code is a deliberate, reviewable change, not
   something to bundle silently into a "prepare" request).
2. That change ships with its own tests (mirroring
   `tests/test_wallet.py`'s coverage of `EXECUTION_ENABLED_IN_CODE`) and,
   by default, all three new gates stay `False`/unset -- nothing changes
   about current behavior until you flip them.
3. **Alpaca account setup:** a *live* (not paper) Alpaca account, API
   keys for the live endpoint, and Alpaca's own account funding.
4. **Position sizing for a small pilot:** set a very small
   `STOCKS_MAX_POSITION_SIZE_USD` / equivalent risk-per-trade cap (well
   below the paper-trading defaults) specifically for the pilot, so a
   worst-case loss stays small in absolute dollars even if every gate is
   open and every trade loses.
5. **You flip all three gates yourself** (edit
   `STOCKS_EXECUTION_ENABLED_IN_CODE` in source, set `STOCKS_LIVE_TRADING`
   and `STOCKS_CONFIRM_LIVE_TRADING` in the environment) -- this project's
   standing rule is that this step is never automated and never done by
   me without your explicit, separate go-ahead at that moment.
6. **Monitor the pilot closely** using the existing dashboard/health
   system (already live-data-only, already covers P&L, drawdown, risk
   state) before any size increase.

## 5. Remaining risks (technical, not financial-advice)

- Backtest/paper-trading performance, even with the trailing-stop fidelity
  fix and walk-forward/OOS discipline applied this session, is still a
  simulation; live fills, slippage, and Alpaca live-endpoint behavior
  (halts, partial fills, live order rejects) are untested because no live
  order has ever been placed by this codebase. This class of bug cannot
  occur yet -- but it becomes the primary review focus once the gated
  execution path is actually written.
- `fold_stability` for breakout sits exactly at the 0.60 pass threshold,
  not comfortably above it -- worth re-checking after a few more months of
  data before increasing size past the initial pilot.
- No live order-execution code exists, so none of the gate flags above
  exist to test yet -- they are specified here as a template, not yet
  wired or exercised by any test.

## 6. Bottom line

Nothing in this document changes runtime behavior. `STOCKS_LIVE_TRADING`
stays `False`, `ALPACA_TRADING_BASE_URL` stays hardcoded to the paper
endpoint, and no code path to Alpaca's live endpoint exists anywhere in
this repository.
