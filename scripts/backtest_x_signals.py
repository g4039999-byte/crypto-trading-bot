"""Validate the X-signal learning mechanism (src.x_account_reputation)
against SYNTHETIC scenarios -- not real collected X data, because none
exists yet (this project has never had a configured X_BEARER_TOKEN; see
src/config.py's X_* block for why: X's API has no free tier as of 2026).

This is deliberately NOT a claim that these specific numbers reflect
real account behavior. It exists to prove the learning mechanism itself
does what it's supposed to -- an account whose signals repeatedly
precede a real, profitable move should end up trusted more than an
account whose signals are noise/spam -- using controlled, labeled
scenarios instead of pretending to have real historical outcomes this
project has not actually observed yet.

Once real signals + real paper-trade outcomes accumulate in
data/x_account_reputation.json (via src.paper_trader recording each
trade's outcome back against the entity's contributing accounts -- see
record_paper_trade_outcome() below, which src/paper_trader.py can call
once a trade tied to an X signal closes), re-run this same kind of
analysis against the real history instead.

Usage:
    python -m scripts.backtest_x_signals
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import x_account_reputation as reputation  # noqa: E402

# Each scenario: an author, and a sequence of past outcomes for signals
# they posted (True = preceded a real, profitable move; False = did
# not -- noise, spam, or a move that didn't materialize). Entirely
# synthetic and clearly labeled as such in the output below.
SYNTHETIC_SCENARIOS = {
    "consistently_early_and_right": [True] * 8 + [False] * 1,
    "spam_account": [False] * 10,
    "mixed_but_net_positive": [True, False, True, True, False, True, False, True],
    "used_to_be_good_now_stale": [True] * 8 + [False] * 8,  # reputation should drift back toward neutral
    "brand_new_no_history": [],
}


def run():
    print("=== X account reputation learning: synthetic scenarios ===")
    print("(Not real data -- see this file's module docstring for why.)\n")

    for account, outcomes in SYNTHETIC_SCENARIOS.items():
        before = reputation.get_weight(account)
        for outcome in outcomes:
            reputation.record_outcome(account, was_useful=outcome, context={"scenario": "synthetic_backtest"})
        after = reputation.get_weight(account)
        print(f"{account:<30} outcomes={len(outcomes):<3} weight {before:.2f} -> {after:.2f}")

    print("\n=== Ranking after all scenarios (top_accounts, min_outcomes=3) ===")
    for entry in reputation.top_accounts(limit=10, min_outcomes=3):
        print(f"{entry['author_id']:<30} weight={entry['weight']:.2f}  outcomes={entry['outcomes_recorded']}")

    print(
        "\nExpected shape: consistently_early_and_right ends highest, "
        "mixed_but_net_positive lands above neutral (1.0) but below it, "
        "and used_to_be_good_now_stale ends clearly BELOW neutral -- its "
        "raw True/False counts are equal, but the 8 False outcomes are "
        "the most recent ones, and EMA smoothing weights recent history "
        "more heavily than old history of the same length."
    )


if __name__ == "__main__":
    run()
