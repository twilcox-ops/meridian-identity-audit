"""Shared audit-trail entry shape and writer for Part B write paths.

Both `onboarding.py` and `offboarding.py` need the same "who ran it, what
changed, before and after" record shape - pulled out here once rather than
each module defining its own near-identical dataclass and writer, which is
exactly the kind of duplication a prior review of this project called out
in the test suite and shouldn't be repeated in the actual write paths.
Each module still owns *where* its entries get written (its own default
`logs/*.jsonl` path), just not the entry shape or the append logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    operator: str
    action: str
    dry_run: bool
    target: str
    before: dict | None
    after: dict | None
    result: str  # e.g. "simulated" | "success" | "failed" | "aborted" | "not_automated"


def record_audit_entry(entry: AuditEntry, path: Path) -> None:
    """Log an audit entry and append it as one JSON line to the audit file."""
    logger.info("AUDIT %s", json.dumps(asdict(entry)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry)) + "\n")
