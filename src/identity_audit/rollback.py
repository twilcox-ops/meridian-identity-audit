"""Shared rollback engine for onboarding.py and offboarding.py.

Both modules need the same three things: find the audit-trail entries for
one prior run, reverse whichever of them are both reversible and actually
succeeded, and report the rest (non-reversible actions, and anything that
wasn't a success in the first place) honestly rather than silently. Pulled
out here once - each module only supplies its own `REVERSERS` mapping
(action name -> the Graph call that undoes it) and its own default audit
log path; the engine itself doesn't know or care whether it's undoing an
onboarding or an offboarding.

## Identifying "which run"

Every entry from one `onboard_user()`/`offboard_user()` call already
shares the same `timestamp` string - that's the run identifier, no schema
change needed. `find_run_entries()` takes an explicit `timestamp` when you
want a specific past run, or defaults to the most recent run recorded for
the given user. The resolved timestamp is always returned so "which run"
is never ambiguous even when the caller didn't specify one.

## Reversing user creation specifically

`onboarding.py`'s `create_user` reverser disables the account rather than
deleting it - a deliberate, documented choice (see that module), not
something decided here.

## Order

Reversal happens in reverse of application order (undo the last action
first) - the more generally defensible default for any rollback, though
in practice none of the reversible actions here (enable state, group
membership, license) depend on each other's order at the Graph API level.

## Rollback-generated entries are never themselves a rollback target

A rollback's own actions are logged with `rollback_`-prefixed action
names, into the *same* audit log the original run wrote to. Found via live
testing: without excluding them, `find_run_entries()`'s "most recent run
for this user" default could select a *prior rollback attempt's* own
entries instead of the real original run - e.g. dry-run a rollback (which
still writes `rollback_*` entries), then run a real rollback right after,
and it would try to "roll back the rollback," producing action names like
`rollback_rollback_create_user` and reporting everything "not reversible"
since those doubly-prefixed names match nothing in any `REVERSERS` map.
Zero real Graph calls happened, but nothing got rolled back either - a
silent no-op dressed up as output, not a crash, which is what made it easy
to miss until it was actually run twice in a row.

Fixed by excluding any entry whose `action` starts with `rollback_` at
read time, before it's ever eligible to be grouped into a candidate run -
this blocks both the default "most recent" path and an explicit
`--timestamp` that happens to land on a rollback run, since targeting a
rollback with another rollback isn't a meaningful operation this engine
supports at all, not just a case to deprioritize.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from identity_audit.audit_trail import AuditEntry, record_audit_entry
from identity_audit.graph_client import GraphClient, GraphError

logger = logging.getLogger(__name__)

# (client, resolved_user_id, original_entry) -> None. Raises GraphError on
# failure, same as every other Graph-calling function in this project.
ReverseFn = Callable[[GraphClient, str, AuditEntry], None]

_SUCCESSFUL = "success"

# Every rollback-generated entry's action is prefixed with this - see the
# module docstring for why entries starting with it are never eligible to
# be selected as a rollback target themselves.
ROLLBACK_ACTION_PREFIX = "rollback_"


@dataclass(frozen=True)
class RollbackOutcome:
    action: str
    original_result: str
    rollback_result: str  # "reversed" | "simulated" | "not_reversible" | "skipped_not_successful" | "failed"


class RollbackTargetNotFound(RuntimeError):
    """No matching audit-trail entries were found for the given user/timestamp."""


def find_run_entries(
    audit_log_path: Path,
    user_principal_name: str,
    timestamp: str | None = None,
) -> tuple[str, list[AuditEntry]]:
    """Load one run's audit entries for a user from a JSONL audit log.

    Returns `(resolved_timestamp, entries)`. If `timestamp` is omitted, the
    most recent run recorded for that user is used - `resolved_timestamp`
    tells the caller which one that was, so it can always be reported.
    Raises `RollbackTargetNotFound` if nothing matches.

    Entries whose `action` starts with `rollback_` are excluded entirely,
    before they're ever grouped into a candidate run - see module
    docstring. Without this, a prior rollback attempt's own log entries
    (dry-run or real) could be selected as "the most recent run" and a
    second rollback would try to reverse the rollback instead of the
    original action.
    """
    if not audit_log_path.exists():
        raise RollbackTargetNotFound(f"No audit log found at {audit_log_path}")

    entries_by_timestamp: dict[str, list[AuditEntry]] = {}
    with open(audit_log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if raw.get("target") != user_principal_name:
                continue
            if raw.get("action", "").startswith(ROLLBACK_ACTION_PREFIX):
                continue
            entry = AuditEntry(**raw)
            entries_by_timestamp.setdefault(entry.timestamp, []).append(entry)

    if not entries_by_timestamp:
        raise RollbackTargetNotFound(
            f"No audit entries found for {user_principal_name} in {audit_log_path}"
        )

    resolved_timestamp = timestamp or max(entries_by_timestamp)
    if resolved_timestamp not in entries_by_timestamp:
        raise RollbackTargetNotFound(
            f"No audit entries found for {user_principal_name} at "
            f"{resolved_timestamp} in {audit_log_path}"
        )

    return resolved_timestamp, entries_by_timestamp[resolved_timestamp]


def run_rollback(
    client: GraphClient,
    entries: list[AuditEntry],
    reversers: dict[str, ReverseFn],
    resolve_user_id_url: str,
    operator: str,
    dry_run: bool,
    audit_log_path: Path,
    now: datetime | None = None,
) -> list[RollbackOutcome]:
    """Reverse every reversible, successful entry in `entries`.

    `resolve_user_id_url` is a full `GET`-able URL (e.g.
    `{GRAPH_BASE_URL}/users/{upn}`) used to resolve the target's object id
    once, up front - every reverser needs it, so it's resolved here rather
    than once per action. Skipped entirely when `dry_run=True`, matching
    every other write path's zero-real-calls dry-run guarantee.

    Raises `GraphError` if that resolution itself fails in a real run -
    nothing can be reversed without it, so that's fatal, not partial.
    """
    if not entries:
        return []

    target = entries[0].target
    reference_time = now or datetime.now(timezone.utc)
    timestamp = reference_time.isoformat(timespec="seconds")
    outcomes: list[RollbackOutcome] = []

    def audit(action: str, before, after, result: str) -> None:
        entry = AuditEntry(
            timestamp=timestamp,
            operator=operator,
            action=f"rollback_{action}",
            dry_run=dry_run,
            target=target,
            before=before,
            after=after,
            result=result,
        )
        record_audit_entry(entry, path=audit_log_path)

    user_id: str | None = None
    if not dry_run:
        response = client.get(resolve_user_id_url, params={"$select": "id"})
        user_id = response.json().get("id")

    for entry in reversed(entries):
        reverser = reversers.get(entry.action)

        if reverser is None:
            outcomes.append(RollbackOutcome(entry.action, entry.result, "not_reversible"))
            audit(entry.action, before=None, after=None, result="not_reversible")
            continue

        if entry.result != _SUCCESSFUL:
            outcomes.append(
                RollbackOutcome(entry.action, entry.result, "skipped_not_successful")
            )
            audit(entry.action, before=None, after=None, result="skipped_not_successful")
            continue

        if dry_run:
            outcomes.append(RollbackOutcome(entry.action, entry.result, "simulated"))
            audit(entry.action, before=entry.after, after=entry.before, result="simulated")
            continue

        try:
            reverser(client, user_id, entry)
            outcomes.append(RollbackOutcome(entry.action, entry.result, "reversed"))
            audit(entry.action, before=entry.after, after=entry.before, result="reversed")
        except GraphError as exc:
            outcomes.append(RollbackOutcome(entry.action, entry.result, "failed"))
            audit(entry.action, before=entry.after, after=None, result="failed")
            logger.error("Rollback of %s failed, continuing: %s", entry.action, exc)

    return outcomes
