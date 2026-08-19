"""Tests for Part B onboarding writes.

Proves: dry-run mode makes zero real Graph calls, a real run (with
confirmation) does make them, and the audit trail captures the required
fields (who ran it, what changed, before/after). Also proves the
confirmation gate itself - CLI `main()`'s argument parsing and
confirm-or-abort flow - since that's the most safety-critical code in the
project and previously had zero automated coverage: a matching
confirmation proceeds, a mismatched one aborts before any Graph call, and
dry-run mode never prompts at all. No real tenant, no real credentials, no
real stdin (the injectable `confirm_fn` is used throughout). All
names/UPNs/group IDs below are fictional placeholders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import identity_audit.onboarding as onboarding
from identity_audit.graph_client import GraphClient
from identity_audit.onboarding import (
    AuditEntry,
    OnboardingResult,
    load_department_group_mapping,
    onboard_user,
    record_audit_entry,
)

NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)

FICTIONAL_DEPARTMENT_GROUPS = {
    "Fictional Engineering": ["group-eng-1", "group-eng-2"],
}


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
        self.calls = []  # (url, json_body)

    def post(self, url, headers=None, json=None):
        self.calls.append((url, json))
        return self._responses.pop(0)

    def get(self, url, headers=None, params=None):  # pragma: no cover - unused here
        raise AssertionError("onboarding should never issue a GET")


def _client(session: FakeSession) -> GraphClient:
    return GraphClient(access_token="fake-token", session=session, sleep=lambda _: None)


def test_dry_run_makes_no_real_api_calls(tmp_path):
    session = FakeSession([])  # any .post() call would raise IndexError - proves none happened
    client = _client(session)

    result = onboard_user(
        client,
        display_name="Fictional Newhire",
        user_principal_name="fictional.newhire@example.test",
        department="Fictional Engineering",
        license_sku_id="fictional-sku-id",
        department_groups=FICTIONAL_DEPARTMENT_GROUPS,
        operator="fictional.operator",
        dry_run=True,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert session.calls == []
    assert result.user_id is None
    assert result.temporary_password is None
    # create_user + add_to_group (x2, one per group in the fixture) + assign_license
    assert [e.result for e in result.entries] == ["simulated"] * 4
    assert [e.action for e in result.entries] == [
        "create_user",
        "add_to_group",
        "add_to_group",
        "assign_license",
    ]


def test_real_run_makes_expected_api_calls(tmp_path):
    session = FakeSession(
        [
            FakeResponse(201, {"id": "new-user-id"}),  # create user
            FakeResponse(204),  # add to group-eng-1
            FakeResponse(204),  # add to group-eng-2
            FakeResponse(200, {}),  # assign license
        ]
    )
    client = _client(session)

    result = onboard_user(
        client,
        display_name="Fictional Newhire",
        user_principal_name="fictional.newhire@example.test",
        department="Fictional Engineering",
        license_sku_id="fictional-sku-id",
        department_groups=FICTIONAL_DEPARTMENT_GROUPS,
        operator="fictional.operator",
        dry_run=False,
        audit_log_path=tmp_path / "audit.jsonl",
        now=NOW,
    )

    assert len(session.calls) == 4
    create_url, create_body = session.calls[0]
    assert create_url.endswith("/users")
    assert create_body["userPrincipalName"] == "fictional.newhire@example.test"
    assert create_body["passwordProfile"]["password"] == result.temporary_password

    group1_url, group1_body = session.calls[1]
    assert group1_url.endswith("/groups/group-eng-1/members/$ref")
    assert group1_body["@odata.id"].endswith("/directoryObjects/new-user-id")

    group2_url, _ = session.calls[2]
    assert group2_url.endswith("/groups/group-eng-2/members/$ref")

    license_url, license_body = session.calls[3]
    assert license_url.endswith("/users/new-user-id/assignLicense")
    assert license_body["addLicenses"] == [{"skuId": "fictional-sku-id"}]

    assert result.user_id == "new-user-id"
    assert result.temporary_password is not None
    assert [e.result for e in result.entries] == ["success", "success", "success", "success"]


def test_audit_trail_captures_required_fields(tmp_path):
    audit_path = tmp_path / "onboarding-audit.jsonl"
    session = FakeSession([])
    client = _client(session)

    onboard_user(
        client,
        display_name="Fictional Newhire",
        user_principal_name="fictional.newhire@example.test",
        department="Fictional Engineering",
        license_sku_id="fictional-sku-id",
        department_groups=FICTIONAL_DEPARTMENT_GROUPS,
        operator="fictional.operator",
        dry_run=True,
        audit_log_path=audit_path,
        now=NOW,
    )

    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    # create_user + add_to_group (x2, one per group in the fixture) + assign_license
    assert len(lines) == 4
    records = [json.loads(line) for line in lines]

    create_record = records[0]
    assert create_record["operator"] == "fictional.operator"
    assert create_record["action"] == "create_user"
    assert create_record["dry_run"] is True
    assert create_record["target"] == "fictional.newhire@example.test"
    assert create_record["before"] is None
    assert create_record["after"]["userPrincipalName"] == "fictional.newhire@example.test"
    assert create_record["result"] == "simulated"
    assert "timestamp" in create_record

    # No plaintext password anywhere in any audit record.
    for record in records:
        assert "password" not in json.dumps(record).lower()


def test_record_audit_entry_appends_json_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    entry = AuditEntry(
        timestamp="2024-06-01T00:00:00+00:00",
        operator="fictional.operator",
        action="create_user",
        dry_run=True,
        target="fictional.newhire@example.test",
        before=None,
        after={"userPrincipalName": "fictional.newhire@example.test"},
        result="simulated",
    )

    record_audit_entry(entry, path=path)
    record_audit_entry(entry, path=path)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "create_user"


def test_load_department_group_mapping_skips_comment_keys(tmp_path):
    config_path = tmp_path / "department_groups.json"
    config_path.write_text(
        json.dumps(
            {
                "_comment": "not a department",
                "Fictional Engineering": ["group-eng-1"],
            }
        ),
        encoding="utf-8",
    )

    mapping = load_department_group_mapping(config_path)

    assert mapping == {"Fictional Engineering": ["group-eng-1"]}


# --- Confirmation gate (main()'s argument-parsing + confirm-or-abort flow) ---
#
# These test main() itself, not just onboard_user() - the gate that decides
# whether onboard_user() ever gets called with dry_run=False at all is the
# actual safety mechanism, and it had no coverage before this. Graph auth
# and onboard_user are monkeypatched to raise if called where the test
# asserts they must not be, which is a hard proof of "no Graph call
# happened" rather than an inference from the outcome.


def _fail_if_called(*args, **kwargs):
    raise AssertionError("should not have been called")


_BASE_ARGV = [
    "--display-name", "Fictional Newhire",
    "--user-principal-name", "fictional.newhire@example.test",
    "--department", "Fictional Engineering",
    "--license-sku-id", "fictional-sku-id",
    "--operator", "fictional.operator",
]


def _write_fictional_config(tmp_path):
    config_path = tmp_path / "department_groups.json"
    config_path.write_text(
        json.dumps({"Fictional Engineering": ["group-eng-1"]}), encoding="utf-8"
    )
    return config_path


def test_main_matching_confirmation_proceeds_with_dry_run_false(monkeypatch, tmp_path):
    config_path = _write_fictional_config(tmp_path)
    captured: dict = {}

    def fake_onboard_user(client, **kwargs):
        captured["client"] = client
        captured.update(kwargs)
        return OnboardingResult(user_id="new-user-id", temporary_password="fake-pw", entries=[])

    monkeypatch.setattr(onboarding, "load_graph_config", lambda: object())
    monkeypatch.setattr(onboarding, "get_access_token", lambda config: "fake-token")
    monkeypatch.setattr(onboarding, "onboard_user", fake_onboard_user)

    exit_code = onboarding.main(
        _BASE_ARGV + ["--department-groups", str(config_path), "--execute"],
        confirm_fn=lambda prompt: "fictional.newhire@example.test",
    )

    assert exit_code == 0
    assert captured["dry_run"] is False
    assert captured["user_principal_name"] == "fictional.newhire@example.test"


def test_main_mismatched_confirmation_aborts_before_any_graph_call(monkeypatch, tmp_path):
    config_path = _write_fictional_config(tmp_path)

    monkeypatch.setattr(onboarding, "load_graph_config", _fail_if_called)
    monkeypatch.setattr(onboarding, "get_access_token", _fail_if_called)
    monkeypatch.setattr(onboarding, "onboard_user", _fail_if_called)

    captured_entries: list[AuditEntry] = []
    monkeypatch.setattr(
        onboarding,
        "record_audit_entry",
        lambda entry, path=None: captured_entries.append(entry),
    )

    exit_code = onboarding.main(
        _BASE_ARGV + ["--department-groups", str(config_path), "--execute"],
        confirm_fn=lambda prompt: "not-the-right-upn",
    )

    assert exit_code == 1
    assert len(captured_entries) == 1
    entry = captured_entries[0]
    assert entry.action == "onboarding_aborted"
    assert entry.result == "aborted"
    assert entry.dry_run is False
    assert entry.target == "fictional.newhire@example.test"


def test_main_dry_run_never_prompts_for_confirmation(monkeypatch, tmp_path):
    config_path = _write_fictional_config(tmp_path)
    captured: dict = {}

    def fake_onboard_user(client, **kwargs):
        captured.update(kwargs)
        return OnboardingResult(user_id=None, temporary_password=None, entries=[])

    # No --execute in argv below, so none of these three should ever run -
    # proves dry-run skips both the confirmation prompt and real auth.
    monkeypatch.setattr(onboarding, "load_graph_config", _fail_if_called)
    monkeypatch.setattr(onboarding, "get_access_token", _fail_if_called)
    monkeypatch.setattr(onboarding, "onboard_user", fake_onboard_user)

    exit_code = onboarding.main(
        _BASE_ARGV + ["--department-groups", str(config_path)],
        confirm_fn=_fail_if_called,
    )

    assert exit_code == 0
    assert captured["dry_run"] is True
