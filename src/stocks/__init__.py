"""US stock paper-trading system -- a separate subsystem from the
Solana/meme-coin one under src/*.py (radar.py, paper_trader.py, etc.).

Deliberately isolated: its own config (src/stocks/config.py), its own
state files (data/stocks/*.json, never data/paper_positions.json or
any other crypto-side file), and its own dashboard routes
(webapp/app.py's /api/stocks/*). Nothing in src/stocks reads or writes
crypto state, and nothing in the crypto modules imports src/stocks --
either one can be broken, disabled, or removed without affecting the
other. src.x_intelligence (X/social signals) is the one module reused
by both, unchanged, because it was already generic (symbol-based, not
Solana-specific).

Paper trading only. LIVE_TRADING_STOCKS (src/stocks/config.py) stays
False; there is no code path here that can place a real brokerage
order regardless of what's configured.
"""
