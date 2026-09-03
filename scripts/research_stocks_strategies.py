"""Large-scale historical research pipeline CLI -- runs src.stocks.
research_pipeline.run_research() across (by default) ~10 years of real
daily bars for the whole configured universe, checks parameter
sensitivity on the leading strategy, and writes both a full JSON report
(data/stocks/research/, local-only) and a human-readable Markdown
summary (STOCKS_LIVE_READINESS_REPORT.md at the project root, meant to
be committed and read directly).

This script can OPTIONALLY record a new strategy version and/or switch
which strategy the live PAPER-trading loop uses (--record / --activate)
-- both act purely on src.stocks.strategy_registry, which has no
connection whatsoever to real money: STOCKS_LIVE_TRADING is hard-set
False in src/stocks/config.py and nothing in this script (or anywhere
else in src/stocks) can change that. Reaching a "LIVE_CANDIDATE"
verdict here is a statement about backtest quality, not a decision
about live trading -- that always requires a separate, explicit human
approval this script cannot grant.

Usage:
    python -m scripts.research_stocks_strategies
    python -m scripts.research_stocks_strategies --lookback-days 1825 --folds 4
    python -m scripts.research_stocks_strategies --record --activate
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.stocks.config import RESEARCH_LOOKBACK_DAYS, RESEARCH_WALK_FORWARD_FOLDS, STOCKS_UNIVERSE  # noqa: E402
from src.stocks.research_pipeline import parameter_sensitivity_check, run_research  # noqa: E402

REPORT_DIR = PROJECT_ROOT / "data" / "stocks" / "research"
MARKDOWN_REPORT_PATH = PROJECT_ROOT / "STOCKS_LIVE_READINESS_REPORT.md"

# A small, deliberately non-exhaustive grid per strategy -- enough to
# demonstrate the leading strategy's result isn't a razor's-edge
# artifact of one specific threshold, without turning this into a
# combinatorial parameter-mining exercise (see research_pipeline.py's
# parameter_sensitivity_check docstring on why stability, not raw
# return, breaks ties in this grid).
_PARAM_GRIDS = {
    "breakout": {"MIN_RELATIVE_VOLUME": [1.2, 1.5, 2.0, 2.5], "NEAR_HIGH_PCT_THRESHOLD": [-0.25, -0.5, -1.0]},
    "momentum": {"MIN_RELATIVE_VOLUME": [1.0, 1.1, 1.3], "MAX_RSI": [72.0, 78.0, 85.0]},
    "pullback": {"MAX_DISTANCE_BELOW_EMA20_PCT": [-1.0, -1.5, -2.0]},
    "relative_volume": {"MIN_RELATIVE_VOLUME": [1.5, 2.0, 2.5, 3.0]},
    "mean_reversion": {"RSI_OVERSOLD": [30.0, 35.0, 40.0]},
}


def _fmt(value, digits=2):
    if value is None:
        return "—"
    if isinstance(value, float) and value == float("inf"):
        return "∞"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}" if isinstance(value, float) else str(value)
    return str(value)


def _print_summary(report):
    print(f"\n=== Research run: {report['universe_size']} symbols, {report['lookback_days']} lookback days, {report['n_folds']} folds ===")
    print(f"Completed in {report['elapsed_seconds']}s -- {report['total_trades_across_all_strategies']} total resolved trades across all strategies\n")

    print("--- Baselines (combined) ---")
    for name, m in report["baselines"].items():
        print(f"  {name:<20} n={m['trade_count']:<5} win%={_fmt(m['win_rate_pct'])} PF={_fmt(m['profit_factor'])} "
              f"expectancy%={_fmt(m['expectancy_pct'])} sharpe={_fmt(m['sharpe'])} maxDD%={_fmt(m['max_drawdown_pct'])}")

    print("\n--- Strategy ranking (best first) ---")
    for row in report["ranking"]:
        sig = "✓" if row["statistically_significant"] else "✗ (not enough trades)"
        print(f"  {row['strategy']:<18} significant={sig:<22} oos_n={row['out_of_sample_trade_count']:<4} "
              f"oos_PF={_fmt(row['out_of_sample_profit_factor'])} oos_expectancy%={_fmt(row['out_of_sample_expectancy_pct'])} "
              f"fold_stability={row['fold_stability_score']}")

    print("\n--- Live-readiness verdicts ---")
    for name, verdict in report["live_readiness"].items():
        failed = [k for k, v in verdict["criteria"].items() if not v["pass"]]
        print(f"  {name:<18} {verdict['verdict']:<15} {'(failing: ' + ', '.join(failed) + ')' if failed else ''}")

    print(f"\n{report['survivorship_bias_disclosure']}")
    print(f"Costs modeled: {report['costs_modeled']['slippage_bps']} bps slippage, "
          f"${report['costs_modeled']['commission_per_trade_usd']} commission/trade.\n")


def _write_markdown_report(report, sensitivity, leading_strategy):
    lines = [
        "# US Stocks Strategy -- Live Readiness Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "**PAPER TRADING ONLY. `STOCKS_LIVE_TRADING` is hard-set `False` in "
        "`src/stocks/config.py` and nothing in this project can change that "
        "programmatically. Reaching a `LIVE_CANDIDATE` verdict below is a "
        "statement about historical backtest quality -- it is NOT a decision "
        "to trade real money, and never triggers one. A human must explicitly "
        "review this report and separately decide whether, when, and how to "
        "ever enable live trading -- no code in this repository can do that "
        "on its own.**",
        "",
        "## Summary",
        "",
        f"- Universe: {report['universe_size']} symbols",
        f"- Lookback: {report['lookback_days']} days (~{report['lookback_days'] / 365:.1f} years)",
        f"- Walk-forward folds: {report['n_folds']}",
        f"- Total resolved historical trades (all strategies combined): **{report['total_trades_across_all_strategies']}**",
        f"- Costs modeled: {report['costs_modeled']['slippage_bps']} bps slippage, "
        f"${report['costs_modeled']['commission_per_trade_usd']} commission/trade (round-trip)",
        f"- Ranked #1 by this run's ranking (significance, then fold-stability, then out-of-sample "
        f"expectancy/PF): **{report['ranking'][0]['strategy'] if report['ranking'] else '—'}**. This is NOT "
        "necessarily the strategy actually active in paper trading -- see `python -m src.stocks.strategy_registry "
        "list` for the live active strategy and the specific rationale recorded for it (a higher fold-stability "
        "score alone doesn't always outweigh a substantially higher per-trade edge; see that rationale for the "
        "actual reasoning behind whichever strategy is active).",
        "",
        "## Baselines",
        "",
        "| Baseline | Trades | Win% | PF | Expectancy% | Sharpe | Sortino | MaxDD% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, m in report["baselines"].items():
        lines.append(f"| {name} | {m['trade_count']} | {_fmt(m['win_rate_pct'])} | {_fmt(m['profit_factor'])} | "
                      f"{_fmt(m['expectancy_pct'])} | {_fmt(m['sharpe'])} | {_fmt(m['sortino'])} | {_fmt(m['max_drawdown_pct'])} |")

    lines += ["", "## Strategy ranking", "",
              "| Strategy | Significant | Combined N | OOS N | OOS PF | OOS Expectancy% | Fold Stability | Beats B&H (Sharpe) |",
              "|---|---|---|---|---|---|---|---|"]
    for row in report["ranking"]:
        lines.append(f"| {row['strategy']} | {'✓' if row['statistically_significant'] else '✗'} | "
                      f"{row['combined_trade_count']} | {row['out_of_sample_trade_count']} | "
                      f"{_fmt(row['out_of_sample_profit_factor'])} | {_fmt(row['out_of_sample_expectancy_pct'])} | "
                      f"{row['fold_stability_score']} | {'✓' if row['beats_buy_and_hold_on_sharpe'] else '✗'} |")

    lines += ["", "## In-sample / Out-of-sample / Walk-forward detail (per strategy)", ""]
    for name, s in report["strategies"].items():
        lines += [f"### {name}", ""]
        for label, key in (("Combined", "combined"), ("In-sample", "in_sample"), ("Out-of-sample", "out_of_sample")):
            m = s[key]
            lines.append(f"- **{label}**: n={m['trade_count']}, win%={_fmt(m['win_rate_pct'])}, PF={_fmt(m['profit_factor'])}, "
                          f"expectancy%={_fmt(m['expectancy_pct'])}, Sharpe={_fmt(m['sharpe'])}, Sortino={_fmt(m['sortino'])}, "
                          f"maxDD%={_fmt(m['max_drawdown_pct'])}")
        lines.append(f"- **Fold stability score**: {s['fold_stability_score']} "
                      f"(fraction of the {report['n_folds']} walk-forward folds where this strategy was both profitable and PF>1)")
        if s["per_regime"]:
            lines.append("- **By market regime** (buckets with ≥10 trades only):")
            for regime, m in s["per_regime"].items():
                lines.append(f"  - {regime}: n={m['trade_count']}, expectancy%={_fmt(m['expectancy_pct'])}, PF={_fmt(m['profit_factor'])}")
        else:
            lines.append("- **By market regime**: no bucket reached the 10-trade minimum sample yet")
        readiness = report["live_readiness"][name]
        lines.append(f"- **Live-readiness verdict**: **{readiness['verdict']}**")
        for crit_name, crit in readiness["criteria"].items():
            lines.append(f"  - {'✓' if crit['pass'] else '✗'} {crit_name}: {_fmt(crit['value'])} (threshold {_fmt(crit['threshold'])})")
        lines.append("")

    if sensitivity:
        lines += [f"## Parameter sensitivity check -- {leading_strategy}", "",
                  "Re-backtested with alternate parameter values (see `scripts/research_stocks_strategies.py`'s "
                  "`_PARAM_GRIDS`), ranked by cross-fold stability first, raw profit factor second -- a config "
                  "that only wins because of one specific threshold, or only in one fold, is exactly what this "
                  "check exists to surface rather than reward.", "",
                  "| Params | Trades | PF | Fold Stability |", "|---|---|---|---|"]
        for r in sensitivity[:8]:
            lines.append(f"| {r['params']} | {r['combined']['trade_count']} | {_fmt(r['combined']['profit_factor'])} | {r['fold_stability_score']} |")
        lines.append("")

    lines += [
        "## Survivorship-bias disclosure", "",
        report["survivorship_bias_disclosure"], "",
        "## What still prevents live trading", "",
        "- `STOCKS_LIVE_TRADING` is hard-set `False` at the source level -- there is no environment "
        "variable, config flag, or CLI argument anywhere in this project that can change it.",
        "- No code path in `src/stocks` submits a real brokerage order; `src/stocks/alpaca_client.py` "
        "only ever calls Alpaca's **paper** endpoint.",
        "- This report is historical-backtest evidence only. It has not been supplemented by weeks of "
        "live paper-trading volume (by design -- see the pipeline's own stated goal of using historical "
        "data for statistical sample size and live paper trading only to validate execution mechanics: "
        "data freshness, signal generation, order simulation, position tracking, stops/targets, restart "
        "recovery, duplicate-trade prevention -- see `tests/stocks/test_paper_broker.py`, "
        "`tests/stocks/test_engine.py`, and this project's live, continuously-running paper loop).",
        "- A human has not yet reviewed and approved this specific report for a live-trading decision.",
        "",
    ]

    MARKDOWN_REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report written to {MARKDOWN_REPORT_PATH}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=RESEARCH_LOOKBACK_DAYS)
    parser.add_argument("--folds", type=int, default=RESEARCH_WALK_FORWARD_FOLDS)
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated override of STOCKS_UNIVERSE")
    parser.add_argument("--skip-sensitivity", action="store_true", help="Skip the parameter sensitivity check (faster)")
    parser.add_argument("--record", action="store_true", help="Record a strategy_registry version for the top-ranked LIVE_CANDIDATE strategy")
    parser.add_argument("--activate", action="store_true", help="Also activate it as the PAPER-trading strategy (no effect on live trading, which has no code path here)")
    args = parser.parse_args()

    from src.logging_config import setup_logging
    setup_logging()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(STOCKS_UNIVERSE)

    report = run_research(symbols=symbols, lookback_days=args.lookback_days, n_folds=args.folds)
    _print_summary(report)

    leading_strategy = report["ranking"][0]["strategy"] if report["ranking"] else None
    sensitivity = None
    if leading_strategy and not args.skip_sensitivity and leading_strategy in _PARAM_GRIDS:
        print(f"Running parameter sensitivity check on {leading_strategy}...")
        sensitivity = parameter_sensitivity_check(leading_strategy, _PARAM_GRIDS[leading_strategy], symbols, args.lookback_days, n_folds=args.folds)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    json_path = REPORT_DIR / f"research_{timestamp}.json"
    json_path.write_text(json.dumps({**report, "parameter_sensitivity": sensitivity, "leading_strategy": leading_strategy}, indent=2, default=str), encoding="utf-8")
    print(f"Full JSON report written to {json_path}")

    _write_markdown_report(report, sensitivity, leading_strategy)

    if args.record or args.activate:
        from src.stocks.strategy_registry import activate_strategy, record_version

        if leading_strategy is None:
            print("No strategy produced any trades -- nothing to record/activate.")
            return report

        verdict = report["live_readiness"][leading_strategy]["verdict"]
        rationale = (
            f"Historical research pipeline ({report['lookback_days']}d lookback, {report['n_folds']} "
            f"walk-forward folds, {report['strategies'][leading_strategy]['combined']['trade_count']} combined trades): "
            f"ranked #1 by significance+stability+out-of-sample expectancy/PF. Live-readiness verdict: {verdict}."
        )
        if args.record:
            record_version(leading_strategy, rationale=rationale)
            print(f"Recorded a new strategy_registry version for {leading_strategy!r}.")
        if args.activate:
            activate_strategy(leading_strategy)
            print(f"Activated {leading_strategy!r} as the PAPER-trading strategy "
                  f"(no effect on live trading -- STOCKS_LIVE_TRADING stays False).")

    print("\n" + "=" * 78)
    print("STOP: this script has NOT enabled, and cannot enable, live trading.")
    print("A human must read STOCKS_LIVE_READINESS_REPORT.md and decide separately")
    print("whether/when to ever approve real-money trading.")
    print("=" * 78)

    return report


if __name__ == "__main__":
    main()
