"""Tests for Part B offboarding writes.

Proves: dry-run makes zero real Graph calls, a real run makes exactly the
expected calls (including filtering directory roles out of "remove from
groups"), failures land in the audit trail without aborting the rest of
the run, a failed user-resolution aborts everything before it starts, and
the confirmation gate (`main()`'s argument-parsing + confirm-or-abort
flow) works the same tested way onboarding's does - a matching
confirmation proceeds, a mismatched one aborts before any Graph call, and
dry-run never prompts at all. No real tenant, no real stdin. All
names/UPNs/group IDs below are fictional placeholders.
"""

from __future__ import annotations

from datetime import datetime, timezone

import identity_audit.offboarding as offboarding
from identity_audit.audit_trail import AuditEntry
from identity_audit.graph_client import GraphClient
from identity_audit.offboarding import OffboardingResult, offboard_user

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)

UPN = "fictional.leaver@example.test"


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


class FakeSession:
    """Records every call, across all four verbs, in call order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []  # (verb, url, payload)

    def get(self, url, headers=None, params=None):
        self.calls.append(("GET", url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return self._responses.pop(0)

    def patch(self, url, headers=None, json=None):
        self.calls.append(("PATCH", url, json))
        return self._responses.pop(0)

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, None))
        return self._responses.pop(0)


def _client(session: FakeSession) -> GraphClient:
    return GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)


def test_dry_run_makes_no_real_api_calls_and_plans_every_action(tmp_path):
    session = FakeSession([])  # any call would raise IndexError - proves none happened
    client = _client(session)

    result = offboard_user(
        client,
        user_principal_name=UPN,
        license_sku_id="fictional-sku-id",
        operator="fictional.operator",
        dry_run=True,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert session.calls == []
    assert result.user_id is None
    assert result.groups_removed == []
    assert [e.action for e in result.entries] == [
        "disable_sign_in",
        "revoke_refresh_tokens",
        "remove_from_groups",
        "reclaim_license",
        "convert_mailbox_to_shared",
    ]
    assert [e.result for e in result.entries] == [
        "simulated",
        "simulated",
        "simulated",
        "simulated",
        "not_automated",
    ]


def test_real_run_makes_expected_calls_and_filters_directory_roles(tmp_path):
    session = FakeSession(
        [
            FakeResponse(200, {"id": "target-user-id", "accountEnabled": True}),  # resolve
            FakeResponse(204),  # disable sign-in
            FakeResponse(200, {"value": True}),  # revoke refresh tokens
            FakeResponse(
                200,
                {
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.group",
                            "id": "group-1",
                            "displayName": "Fictional Team",
                        },
                        {
                            "@odata.type": "#microsoft.graph.directoryRole",
                            "id": "role-1",
                            "displayName": "Fictional Admin Role",
                        },
                    ]
                },
            ),  # memberOf - one group, one directory role
            FakeResponse(204),  # remove from group-1
            FakeResponse(200, {}),  # reclaim license
        ]
    )
    client = _client(session)

    result = offboard_user(
        client,
        user_principal_name=UPN,
        license_sku_id="fictional-sku-id",
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert result.user_id == "target-user-id"
    # Only the group was removed - the directory role was filtered out.
    assert result.groups_removed == ["group-1"]

    verbs_and_urls = [(v, u) for v, u, _ in session.calls]
    assert verbs_and_urls == [
        ("GET", "https://graph.microsoft.com/v1.0/users/fictional.leaver@example.test"),
        ("PATCH", "https://graph.microsoft.com/v1.0/users/target-user-id"),
        ("POST", "https://graph.microsoft.com/v1.0/users/target-user-id/revokeSignInSessions"),
        ("GET", "https://graph.microsoft.com/v1.0/users/target-user-id/memberOf"),
        (
            "DELETE",
            "https://graph.microsoft.com/v1.0/groups/group-1/members/target-user-id/$ref",
        ),
        ("POST", "https://graph.microsoft.com/v1.0/users/target-user-id/assignLicense"),
    ]

    _, _, disable_body = session.calls[1]
    assert disable_body == {"accountEnabled": False}
    _, _, license_body = session.calls[5]
    assert license_body == {"addLicenses": [], "removeLicenses": ["fictional-sku-id"]}

    entries_by_action = {e.action: e for e in result.entries}
    assert entries_by_action["disable_sign_in"].result == "success"
    assert entries_by_action["revoke_refresh_tokens"].result == "success"
    assert entries_by_action["remove_from_groups"].result == "success"
    assert entries_by_action["remove_from_groups"].after == {
        "member_of": False,
        "group_id": "group-1",
    }
    assert entries_by_action["reclaim_license"].result == "success"
    assert entries_by_action["convert_mailbox_to_shared"].result == "not_automated"


def test_real_run_records_failures_without_aborting_remaining_actions(tmp_path):
    session = FakeSession(
        [
            FakeResponse(200, {"id": "target-user-id", "accountEnabled": True}),  # resolve
            FakeResponse(403, {"error": {"code": "Forbidden"}}),  # disable sign-in FAILS
            FakeResponse(200, {"value": True}),  # revoke tokens still attempted, succeeds
            FakeResponse(200, {"value": []}),  # memberOf - no groups
            FakeResponse(200, {}),  # reclaim license still attempted, succeeds
        ]
    )
    client = _client(session)

    result = offboard_user(
        client,
        user_principal_name=UPN,
        license_sku_id="fictional-sku-id",
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    entries_by_action = {e.action: e for e in result.entries}

    disable_entry = entries_by_action["disable_sign_in"]
    assert disable_entry.result == "failed"
    assert disable_entry.before == {"accountEnabled": True}
    assert disable_entry.after is None
    assert disable_entry.operator == "fictional.operator"
    assert disable_entry.target == UPN

    # Failure of one action doesn't stop the rest from being attempted.
    assert entries_by_action["revoke_refresh_tokens"].result == "success"
    assert entries_by_action["reclaim_license"].result == "success"


def test_user_resolution_failure_aborts_before_any_write(tmp_path):
    session = FakeSession([FakeResponse(404, {"error": {"code": "Request_ResourceNotFound"}})])
    client = _client(session)

    result = offboard_user(
        client,
        user_principal_name=UPN,
        license_sku_id="fictional-sku-id",
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert result.user_id is None
    assert result.groups_removed == []
    assert len(session.calls) == 1  # only the failed resolution - nothing else attempted
    assert [e.action for e in result.entries] == ["resolve_user"]
    assert result.entries[0].result == "failed"


# --- Confirmation gate (main()'s argument-parsing + confirm-or-abort flow) ---
#
# Same tested pattern as onboarding's main() - Graph auth and offboard_user
# are monkeypatched to raise if called where a test asserts they must not
# be, which is a hard proof rather than an inference from the outcome.


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not have been called")


_BASE_ARGV = [
    "--user-principal-name", UPN,
    "--license-sku-id", "fictional-sku-id",
    "--operator", "fictional.operator",
]


def test_main_matching_confirmation_proceeds_with_dry_run_false(monkeypatch):
    captured: dict = {}

    def fake_offboard_user(client, **kwargs):
        captured["client"] = client
        captured.update(kwargs)
        return OffboardingResult(user_id="target-user-id", groups_removed=[], entries=[])

    monkeypatch.setattr(offboarding, "load_graph_config", lambda: object())
    monkeypatch.setattr(offboarding, "get_access_token", lambda config: "fake-token")
    monkeypatch.setattr(offboarding, "offboard_user", fake_offboard_user)

    exit_code = offboarding.main(
        _BASE_ARGV + ["--execute"],
        confirm_fn=lambda prompt: UPN,
    )

    assert exit_code == 0
    assert captured["dry_run"] is False
    assert captured["user_principal_name"] == UPN


def test_main_mismatched_confirmation_aborts_before_any_graph_call(monkeypatch):
    monkeypatch.setattr(offboarding, "load_graph_config", _fail_if_called)
    monkeypatch.setattr(offboarding, "get_access_token", _fail_if_called)
    monkeypatch.setattr(offboarding, "offboard_user", _fail_if_called)

    captured_entries: list[AuditEntry] = []
    monkeypatch.setattr(
        offboarding,
        "record_audit_entry",
        lambda entry, path=None: captured_entries.append(entry),
    )

    exit_code = offboarding.main(
        _BASE_ARGV + ["--execute"],
        confirm_fn=lambda prompt: "not-the-right-upn",
    )

    assert exit_code == 1
    assert len(captured_entries) == 1
    entry = captured_entries[0]
    assert entry.action == "offboarding_aborted"
    assert entry.result == "aborted"
    assert entry.dry_run is False
    assert entry.target == UPN


def test_main_dry_run_never_prompts_for_confirmation(monkeypatch):
    captured: dict = {}

    def fake_offboard_user(client, **kwargs):
        captured.update(kwargs)
        return OffboardingResult(user_id=None, groups_removed=[], entries=[])

    # No --execute in argv below, so none of these three should ever run -
    # proves dry-run skips both the confirmation prompt and real auth.
    monkeypatch.setattr(offboarding, "load_graph_config", _fail_if_called)
    monkeypatch.setattr(offboarding, "get_access_token", _fail_if_called)
    monkeypatch.setattr(offboarding, "offboard_user", fake_offboard_user)

    exit_code = offboarding.main(_BASE_ARGV, confirm_fn=_fail_if_called)

    assert exit_code == 0
    assert captured["dry_run"] is True
