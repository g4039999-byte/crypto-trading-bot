"""Real, live connectivity check for X (Twitter) API v2 -- the one test
in this project that is *supposed* to touch the real network, on
purpose, so you can confirm your own X_BEARER_TOKEN actually works
before relying on it. Every other X test (tests/test_x_*.py) is fully
mocked and never makes a real call.

Costs a handful of real post-reads against your X_MAX_READS_PER_DAY
budget if a token is configured (one small search, capped at 10
results -- the API's own minimum). Zero cost, zero network calls, and a
clear message if X_BEARER_TOKEN is not set.

Usage:
    python -m scripts.test_x_connection
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import x_client, x_signal_engine  # noqa: E402
from src.config import X_BEARER_TOKEN, X_MAX_READS_PER_DAY, X_POLL_INTERVAL_SECONDS, X_SEARCH_QUERIES  # noqa: E402


def main():
    print("=== X API connectivity check ===\n")

    if not X_BEARER_TOKEN:
        print("X_BEARER_TOKEN is not set (.env is empty or missing).")
        print("This is a valid, fully-supported state: Radar and Paper Trading")
        print("run identically either way -- X is purely additive.")
        print("\nTo test a real connection: paste your Bearer Token into the")
        print("X_BEARER_TOKEN= line in .env (project root), save, and re-run this.")
        return 0

    masked = X_BEARER_TOKEN[:6] + "…" + X_BEARER_TOKEN[-4:] if len(X_BEARER_TOKEN) > 12 else "…"
    print(f"X_BEARER_TOKEN is set ({masked}). X_ENABLED={x_client.X_ENABLED}.")
    if not x_client.is_configured():
        print("is_configured() is False -- check X_ENABLED in .env (must not be 'false').")
        return 1

    print(f"Daily read budget: {x_client.reads_used_today()}/{X_MAX_READS_PER_DAY} used so far today.")
    print(f"Configured search queries: {list(X_SEARCH_QUERIES)}")
    print(f"Poll interval (radar uses this, not every cycle): {X_POLL_INTERVAL_SECONDS}s\n")

    query = X_SEARCH_QUERIES[0] if X_SEARCH_QUERIES else "solana meme coin"
    print(f"Making one real search_recent({query!r}, max_results=10)...")
    posts = x_client.search_recent(query, max_results=10)

    if not posts:
        print("\nNo posts returned. This can mean: a genuinely empty result, an")
        print("invalid/expired token (check the log above for a 401), a rate limit")
        print("(check for a 429/backoff message above), or a network problem.")
        print("Nothing here is fatal to Radar/Paper Trading either way.")
        return 1 if x_client.reads_used_today() == 0 else 0

    print(f"\nSUCCESS: received {len(posts)} real post(s). Sample:")
    for post in posts[:3]:
        print(f"  - @{post.get('author_username') or '?'}: {post.get('text', '')[:100]!r}")

    print("\nFeeding this batch through x_signal_engine (the same step the radar takes)...")
    touched = x_signal_engine.update_signal_state(posts, query=query)
    if touched:
        print(f"Entities detected: {touched}")
    else:
        print("No cashtag/hashtag/meme-context entities found in this small sample -- try again")
        print("later, or with a more specific query; this is normal for a 10-post sample.")

    print(f"\nReads used today now: {x_client.reads_used_today()}/{X_MAX_READS_PER_DAY}")
    print("\nConnection confirmed working end to end: X API -> x_client -> x_signal_engine.")
    print("The radar will pick this up automatically on its next poll (every")
    print(f"{X_POLL_INTERVAL_SECONDS:.0f}s while it's running) -- no restart required for future polls,")
    print("though a currently-running radar process needs a restart to pick up a")
    print("freshly-added X_BEARER_TOKEN itself (it was read once at process start).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
