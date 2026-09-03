"""Shared logging setup for the radar.

Call setup_logging() once, as early as possible (radar.py's entry point
does this), then use logging.getLogger(__name__) anywhere else in the
codebase. Nothing in this module talks to the network or reads secrets.
"""

import logging
import sys

from src.config import LOG_LEVEL

_CONFIGURED = False


def _force_utf8_console():
    """Reconfigure stdout/stderr to UTF-8 before anything writes to them.

    Root cause of the "radar cycle fails every time" bug: on Windows,
    stdout/stderr default to the console's legacy code page (e.g. cp1256
    for an Arabic Windows locale) -- including when redirected to a log
    file, since a redirected stream still inherits that encoding. Token
    symbols/names scraped from DexScreener routinely contain Unicode
    characters (emoji, exotic scripts) that code page cannot represent,
    so any print() or log line containing one raised UnicodeEncodeError.
    That crashed the cycle before it ever reached the paper-trading
    decision step (run_once()'s print loop runs before on_results()).

    UTF-8 can represent any Unicode code point, so switching to it here
    -- once, for the whole process, before any handler or print() call
    -- removes this entire class of crash everywhere in the codebase,
    not just the one line that happened to surface it first.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # a stream without reconfigure() (e.g. a test runner's capture) -- leave it alone
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # best-effort: never let console setup itself crash startup


def setup_logging(level=None):
    """Configure the root logger once. Safe to call multiple times."""
    global _CONFIGURED

    if _CONFIGURED:
        return

    _force_utf8_console()

    resolved_level = getattr(logging, (level or LOG_LEVEL).upper(), logging.INFO)

    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _CONFIGURED = True
