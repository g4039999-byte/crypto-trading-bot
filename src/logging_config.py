"""Shared logging setup for the radar.

Call setup_logging() once, as early as possible (radar.py's entry point
does this), then use logging.getLogger(__name__) anywhere else in the
codebase. Nothing in this module talks to the network or reads secrets.
"""

import logging

from src.config import LOG_LEVEL

_CONFIGURED = False


def setup_logging(level=None):
    """Configure the root logger once. Safe to call multiple times."""
    global _CONFIGURED

    if _CONFIGURED:
        return

    resolved_level = getattr(logging, (level or LOG_LEVEL).upper(), logging.INFO)

    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    _CONFIGURED = True
