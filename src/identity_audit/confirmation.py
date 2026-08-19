"""Shared typed-confirmation gate for every real (non-dry-run) write path.

Onboarding, offboarding, and rollback all need the same mechanism: retype
the exact subject (a UPN) back before anything real happens. Pulled out
here once rather than three near-identical copies - the same reasoning
that moved `AuditEntry`/`record_audit_entry` into `audit_trail.py`.
"""

from __future__ import annotations

from typing import Callable


def confirm_retype(
    expected_value: str, prompt: str, confirm_fn: Callable[[str], str] = input
) -> bool:
    """True iff `confirm_fn(prompt)` returns exactly `expected_value` (stripped).

    A generic "yes"/"CONFIRM" would be weaker: the risk every write path
    here guards against is running against the wrong user, and retyping
    the specific value forces a conscious re-check of exactly who's being
    acted on. `confirm_fn` is injectable so tests never touch real stdin.
    """
    typed = confirm_fn(prompt)
    return typed.strip() == expected_value
