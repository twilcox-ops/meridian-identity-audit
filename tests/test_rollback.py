"""Tests for the shared rollback engine (identity_audit.rollback).

Module-agnostic - uses small fictional reversers rather than importing
onboarding's or offboarding's real ones, since this tests the engine
itself: finding a run's audit entries, dispatching each to its reverser
(or reporting "not reversible"/"skipped"), and the dry-run/real-call
guarantees. No real tenant. All names below are fictional placeholders.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from identity_audit.audit_trail import AuditEntry
from identity_audit.graph_client import GraphClient, GraphError
from identity_audit.rollback import (
    RollbackOutcome,
    RollbackTargetNotFound,
    find_run_entries,
    run_rollback,
)

UPN = "fictional.user@example.test"
RESOLVE_URL = f"https://graph.microsoft.com/v1.0/users/{UPN}"
NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (verb, url, payload)

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return self._responses.pop(0)

    def patch(self, url, headers=None, json=None):
        self.calls.append(("PATCH", url, json))
        return self._responses.pop(0)


def _client(session: FakeSession) -> GraphClient:
    return GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)


def _reverse_via_patch(client, user_id, entry):
    client.patch(f"https://graph.microsoft.com/v1.0/users/{user_id}", json_body={"restored": True})


def _reverse_via_post(client, user_id, entry):
    client.post(
        f"https://graph.microsoft.com/v1.0/users/{user_id}/reAdd",
        json_body={"group_id": entry.before["group_id"]},
    )


# "fictional_irreversible_action" is deliberately absent - not reversible.
FICTIONAL_REVERSERS = {
    "fictional_disable": _reverse_via_patch,
    "fictional_remove_from_group": _reverse_via_post,
}


def _entry(action, result, before=None, after=None, timestamp="2024-06-01T00:00:00+00:00"):
    return AuditEntry(
        timestamp=timestamp,
        operator="fictional.operator",
        action=action,
        dry_run=False,
        target=UPN,
        before=before,
        after=after,
        result=result,
    )


# --- find_run_entries ---


def test_find_run_entries_defaults_to_most_recent_run_for_user(tmp_path):
    path = tmp_path / "audit.jsonl"
    older = _entry("fictional_disable", "success", timestamp="2024-01-01T00:00:00+00:00")
    newer = _entry("fictional_disable", "success", timestamp="2024-06-01T00:00:00+00:00")
    other_user = AuditEntry(
        timestamp="2024-12-01T00:00:00+00:00",
        operator="fictional.operator",
        action="fictional_disable",
        dry_run=False,
        target="someone.else@example.test",
        before=None,
        after=None,
        result="success",
    )
    with open(path, "a", encoding="utf-8") as f:
        for e in (older, newer, other_user):
            f.write(json.dumps(asdict(e)) + "\n")

    resolved_timestamp, entries = find_run_entries(path, UPN)

    assert resolved_timestamp == "2024-06-01T00:00:00+00:00"
    assert [e.action for e in entries] == ["fictional_disable"]


def test_find_run_entries_honors_explicit_timestamp(tmp_path):
    path = tmp_path / "audit.jsonl"
    older = _entry("fictional_disable", "success", timestamp="2024-01-01T00:00:00+00:00")
    newer = _entry("fictional_disable", "success", timestamp="2024-06-01T00:00:00+00:00")
    with open(path, "a", encoding="utf-8") as f:
        for e in (older, newer):
            f.write(json.dumps(asdict(e)) + "\n")

    resolved_timestamp, entries = find_run_entries(
        path, UPN, timestamp="2024-01-01T00:00:00+00:00"
    )

    assert resolved_timestamp == "2024-01-01T00:00:00+00:00"
    assert len(entries) == 1


def test_find_run_entries_raises_when_nothing_matches(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")

    with pytest.raises(RollbackTargetNotFound):
        find_run_entries(path, UPN)


def test_find_run_entries_raises_when_log_file_does_not_exist(tmp_path):
    with pytest.raises(RollbackTargetNotFound):
        find_run_entries(tmp_path / "does-not-exist.jsonl", UPN)


# --- run_rollback ---


def test_dry_run_rollback_makes_no_real_calls(tmp_path):
    session = FakeSession([])  # any call would raise IndexError - proves none happened
    client = _client(session)
    entries = [
        _entry(
            "fictional_disable",
            "success",
            before={"accountEnabled": True},
            after={"accountEnabled": False},
        ),
        _entry("fictional_irreversible_action", "success"),
    ]

    outcomes = run_rollback(
        client,
        entries,
        reversers=FICTIONAL_REVERSERS,
        resolve_user_id_url=RESOLVE_URL,
        operator="fictional.operator",
        dry_run=True,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert session.calls == []
    results_by_action = {o.action: o.rollback_result for o in outcomes}
    assert results_by_action["fictional_disable"] == "simulated"
    assert results_by_action["fictional_irreversible_action"] == "not_reversible"


def test_real_rollback_reverses_only_reversible_successful_entries(tmp_path):
    session = FakeSession(
        [
            FakeResponse(200, {"id": "target-user-id"}),  # resolve user id
            FakeResponse(200, {}),  # reverse "remove_from_group" (last entry - LIFO, reversed first)
            FakeResponse(204),  # reverse "disable" (first entry - reversed last)
        ]
    )
    client = _client(session)
    entries = [
        _entry(
            "fictional_disable",
            "success",
            before={"accountEnabled": True},
            after={"accountEnabled": False},
        ),
        _entry("fictional_irreversible_action", "success"),  # not in REVERSERS
        _entry("fictional_disable", "failed"),  # original action failed - nothing to reverse
        _entry(
            "fictional_remove_from_group",
            "success",
            before={"group_id": "group-1"},
            after=None,
        ),
    ]

    outcomes = run_rollback(
        client,
        entries,
        reversers=FICTIONAL_REVERSERS,
        resolve_user_id_url=RESOLVE_URL,
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    # Reversed in LIFO order (undo the last thing done first).
    assert outcomes == [
        RollbackOutcome("fictional_remove_from_group", "success", "reversed"),
        RollbackOutcome("fictional_disable", "failed", "skipped_not_successful"),
        RollbackOutcome("fictional_irreversible_action", "success", "not_reversible"),
        RollbackOutcome("fictional_disable", "success", "reversed"),
    ]

    assert len(session.calls) == 3  # resolve + 2 actual reversals - not 4
    resolve_call, post_call, patch_call = session.calls
    assert resolve_call == ("GET", RESOLVE_URL, {"$select": "id"})
    assert post_call == (
        "POST",
        "https://graph.microsoft.com/v1.0/users/target-user-id/reAdd",
        {"group_id": "group-1"},
    )
    assert patch_call == (
        "PATCH",
        "https://graph.microsoft.com/v1.0/users/target-user-id",
        {"restored": True},
    )


def test_rollback_never_targets_a_prior_rollbacks_own_entries(tmp_path):
    """Reproduces the exact bug found via live testing: dry-run rollback
    happens (writes rollback_* entries to the log), then a real rollback
    is requested for the same user - it must find and target the
    *original* action entries, not the rollback's own log entries from a
    moment ago, even though those are now more recent in the same file.
    """
    audit_log_path = tmp_path / "audit.jsonl"
    original_timestamp = "2024-06-01T00:00:00+00:00"
    original_entries = [
        _entry(
            "fictional_disable",
            "success",
            before={"accountEnabled": True},
            after={"accountEnabled": False},
            timestamp=original_timestamp,
        )
    ]
    with open(audit_log_path, "a", encoding="utf-8") as f:
        for e in original_entries:
            f.write(json.dumps(asdict(e)) + "\n")

    # Step 1: a dry-run rollback happens shortly after the original run -
    # this writes real rollback_fictional_disable entries into the same
    # log, with a later timestamp than the original run's.
    later_timestamp = datetime(2024, 6, 1, 0, 5, tzinfo=timezone.utc)
    dry_run_session = FakeSession([])
    run_rollback(
        _client(dry_run_session),
        original_entries,
        reversers=FICTIONAL_REVERSERS,
        resolve_user_id_url=RESOLVE_URL,
        operator="fictional.operator",
        dry_run=True,
        audit_log_path=audit_log_path,
        now=later_timestamp,
    )
    # Confirm the dry-run rollback really did write rollback_-prefixed
    # entries to the log, same as the live bug report described.
    logged_actions = [
        json.loads(line)["action"]
        for line in audit_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "rollback_fictional_disable" in logged_actions

    # Step 2: find_run_entries, called exactly as a real --rollback run
    # would call it (no explicit --timestamp - "most recent for this
    # user") must still resolve to the ORIGINAL run, not the rollback's
    # own (later) entries.
    resolved_timestamp, found_entries = find_run_entries(audit_log_path, UPN)

    assert resolved_timestamp == original_timestamp
    assert [e.action for e in found_entries] == ["fictional_disable"]

    # Step 3: a real rollback using those found entries actually reverses
    # the original action, proving the fix end-to-end, not just at the
    # lookup layer.
    real_session = FakeSession(
        [
            FakeResponse(200, {"id": "target-user-id"}),  # resolve user id
            FakeResponse(204),  # reverse "fictional_disable" (PATCH)
        ]
    )
    outcomes = run_rollback(
        _client(real_session),
        found_entries,
        reversers=FICTIONAL_REVERSERS,
        resolve_user_id_url=RESOLVE_URL,
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=audit_log_path,
        now=datetime(2024, 6, 1, 0, 10, tzinfo=timezone.utc),
    )

    assert outcomes == [RollbackOutcome("fictional_disable", "success", "reversed")]
    assert len(real_session.calls) == 2  # resolve + the one real reversal - not "not_reversible"


def test_real_rollback_raises_if_user_resolution_fails(tmp_path):
    session = FakeSession([FakeResponse(404, {"error": {"code": "NotFound"}})])
    client = _client(session)
    entries = [
        _entry(
            "fictional_disable",
            "success",
            before={"accountEnabled": True},
            after={"accountEnabled": False},
        )
    ]

    with pytest.raises(GraphError):
        run_rollback(
            client,
            entries,
            reversers=FICTIONAL_REVERSERS,
            resolve_user_id_url=RESOLVE_URL,
            operator="fictional.operator",
            dry_run=False,
            audit_log_path=tmp_path / "audit.jsonl",
            now=NOW,
        )
