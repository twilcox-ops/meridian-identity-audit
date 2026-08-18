"""Shared ISO 8601 timestamp parsing for Graph responses.

Graph sometimes returns fractional seconds with more than 6 digits (100-ns
ticks rendered as a decimal string), which `datetime.fromisoformat` rejects.
Every check that parses a Graph timestamp (`signInActivity/lastSignInDateTime`,
`createdDateTime`, ...) goes through this one function so that fix - and any
future one - lives in a single place instead of drifting per-check.
"""

from __future__ import annotations

import re
from datetime import datetime

_EXCESS_FRACTIONAL_SECONDS_RE = re.compile(r"(\.\d{6})\d+")


def parse_graph_datetime(value: str) -> datetime:
    """Parse a Graph ISO 8601 timestamp into an aware UTC datetime."""
    value = value.replace("Z", "+00:00")
    value = _EXCESS_FRACTIONAL_SECONDS_RE.sub(r"\1", value)
    return datetime.fromisoformat(value)
