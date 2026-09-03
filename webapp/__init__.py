"""Local web interface for a non-technical end user.

This package is presentation/control-plane only: it never contains
trading strategy logic. It reads existing state files through the
existing src.* read-only functions (src.paper_portfolio,
src.opportunity_watchlist, src.news_signal_engine, src.config) and
controls trading the same way a human at a terminal already could --
by starting/stopping the existing `python -m src.radar --loop --paper`
process and by using the existing src.kill_switch module. It adds no
new trading rule and never touches src.live_trader or src.wallet.
"""
