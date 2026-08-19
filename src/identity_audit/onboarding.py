"""Part B, first piece: onboarding writes for a simulated new hire.

Create the user, add them to their department's groups, assign a license,
and log every action to an audit trail - dry-run by default, a real run
needs `--execute` plus typing the exact UPN back as confirmation.

## Permissions

Two new application permissions beyond everything Part A uses (all of
which is read-only):

- `User.ReadWrite.All` - `POST /users` (create) and
  `POST /users/{id}/assignLicense` both need this; `User.Read.All` does
  not imply it, Graph treats read and write as separate scope strings.
- `GroupMember.ReadWrite.All` - `POST /groups/{id}/members/$ref` (add to
  group) needs this. It *widens* the read-only `GroupMember.Read.All`
  already granted for the ownerless-groups check, rather than being an
  unrelated new grant - worth documenting as a widening, not a fresh ask.

See README Permissions section once these are live-tested.

## Department -> groups mapping

Loaded from a JSON config file (default `config/department_groups.json`,
gitignored - `config/department_groups.example.json` is the tracked
template) mapping department name -> list of Graph **group object IDs**
(GUIDs), not display names. IDs avoid a name-lookup call and the ambiguity
of two groups sharing a display name; the tradeoff is the config file
itself is less human-readable than names would be, which is why the
example template documents each department inline.

## Dry-run and confirmation

Default (no `--execute`) is a dry run: the full plan is computed and
written to the audit trail with `result="simulated"`, and zero Graph
calls are made - `onboard_user()` never touches `client` when
`dry_run=True`. `--execute` requires the operator to then type the exact
user principal name back when prompted; anything else aborts before any
Graph call, with the abort itself still recorded to the audit trail. A
generic "yes"/"CONFIRM" would be weaker - the actual risk this guards
against (per the project brief) is running against the wrong user, and
retyping the specific UPN forces a conscious re-check of exactly who's
being acted on.

## Audit trail

One entry per action (`create_user`, `add_to_group`, `assign_license`,
plus `onboarding_aborted` for a failed confirmation), each with
`timestamp`, `operator`, `action`, `dry_run`, `target`, `before`, `after`,
`result`. Every entry is logged via the standard logger *and* appended as
one JSON line to a local audit file (default `logs/onboarding-audit.jsonl`)
so it survives past the console/log-stream scrolling away. The generated
temporary password is deliberately never written to the audit trail or
logged anywhere, plaintext or otherwise - only the fact that one was set.

## Rollback

`--rollback` reverses a prior run instead of starting a new one - same
dry-run default, same typed-confirmation gate, same audit trail (entries
written as `rollback_<action>`). Which run: `--timestamp` picks an exact
one (every entry from a single run already shares one timestamp value, so
no new run-identifier scheme was needed); omitted, it defaults to the most
recent run recorded for `--user-principal-name`. The engine itself -
finding the run, dispatching each entry to its reverser or reporting it as
not reversible - lives in `identity_audit.rollback`, shared with
offboarding; only the three reversers below, and what "create" reverses
to, belong to this module.

`create_user`'s reversal **disables the account rather than deleting it**
- a deliberate choice, not the only option: it reuses the exact mechanism
`offboarding.py`'s own `disable_sign_in` already relies on and is already
documented as reversible, rather than introducing `DELETE /users/{id}`
(itself only a *soft* delete, needing its own separate restore call and a
30-day window this project doesn't otherwise touch) as a second,
asymmetric way to remove access. `add_to_group` reverses via the same
`$ref` removal offboarding uses; `assign_license` reverses via
`assignLicense`'s `removeLicenses`.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from identity_audit.audit_trail import AuditEntry, record_audit_entry
from identity_audit.auth import get_access_token
from identity_audit.confirmation import confirm_retype
from identity_audit.config import load_graph_config
from identity_audit.graph_client import GRAPH_BASE_URL, GraphClient, GraphError
from identity_audit.rollback import (
    ReverseFn,
    RollbackTargetNotFound,
    find_run_entries,
    run_rollback,
)

logger = logging.getLogger(__name__)

DEFAULT_DEPARTMENT_GROUPS_PATH = Path("config") / "department_groups.json"
DEFAULT_AUDIT_LOG_PATH = Path("logs") / "onboarding-audit.jsonl"

USERS_PATH = "/users"
_TEMP_PASSWORD_LENGTH = 20


@dataclass(frozen=True)
class OnboardingResult:
    user_id: str | None  # None if dry-run or the create call itself failed
    temporary_password: str | None  # None if dry-run; never logged or audited
    entries: list[AuditEntry]


def load_department_group_mapping(
    path: Path = DEFAULT_DEPARTMENT_GROUPS_PATH,
) -> dict[str, list[str]]:
    """Load department -> [group object ID, ...] from a JSON config file.

    Keys starting with `_` are treated as comments (JSON has no native
    comment syntax) and skipped - see department_groups.example.json.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def _generate_temporary_password() -> str:
    """A random password meeting typical Entra complexity rules.

    Guarantees at least one of each character class rather than trusting
    that to chance, then fills the rest randomly and shuffles.
    """
    classes = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*-_="]
    required = [secrets.choice(c) for c in classes]
    remaining_length = _TEMP_PASSWORD_LENGTH - len(required)
    pool = "".join(classes)
    remaining = [secrets.choice(pool) for _ in range(remaining_length)]
    password_chars = required + remaining
    secrets.SystemRandom().shuffle(password_chars)
    return "".join(password_chars)


def onboard_user(
    client: GraphClient,
    display_name: str,
    user_principal_name: str,
    department: str,
    license_sku_id: str,
    department_groups: dict[str, list[str]],
    operator: str,
    dry_run: bool = True,
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH,
    now: datetime | None = None,
) -> OnboardingResult:
    """Create a user, add them to their department's groups, assign a license.

    `client` is never called when `dry_run=True` - every action is planned
    and audited with `result="simulated"` instead. Group IDs come from
    `department_groups[department]`; an unknown department is treated as
    "no groups to add" rather than an error, since a new department showing
    up before the config is updated is an operator/config gap, not a reason
    to abort user creation entirely.
    """
    reference_time = now or datetime.now(timezone.utc)
    timestamp = reference_time.isoformat(timespec="seconds")
    group_ids = department_groups.get(department, [])
    entries: list[AuditEntry] = []

    def audit(action: str, before: dict | None, after: dict | None, result: str) -> None:
        entry = AuditEntry(
            timestamp=timestamp,
            operator=operator,
            action=action,
            dry_run=dry_run,
            target=user_principal_name,
            before=before,
            after=after,
            result=result,
        )
        entries.append(entry)
        record_audit_entry(entry, path=audit_log_path)

    # --- Create user ---
    planned_user = {
        "userPrincipalName": user_principal_name,
        "displayName": display_name,
        "department": department,
    }
    if dry_run:
        audit("create_user", before=None, after=planned_user, result="simulated")
        temporary_password = None
        user_id = None
    else:
        temporary_password = _generate_temporary_password()
        payload = {
            "accountEnabled": True,
            "displayName": display_name,
            "mailNickname": user_principal_name.split("@")[0],
            "userPrincipalName": user_principal_name,
            "department": department,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": True,
                "password": temporary_password,
            },
        }
        try:
            response = client.post(f"{GRAPH_BASE_URL}{USERS_PATH}", json_body=payload)
            created = response.json()
            user_id = created.get("id")
            audit("create_user", before=None, after=planned_user, result="success")
        except GraphError as exc:
            audit("create_user", before=None, after=None, result="failed")
            logger.error("User creation failed, aborting onboarding: %s", exc)
            return OnboardingResult(user_id=None, temporary_password=None, entries=entries)

    # --- Add to department groups ---
    for group_id in group_ids:
        if dry_run:
            audit(
                "add_to_group",
                before={"member_of": False, "group_id": group_id},
                after={"member_of": True, "group_id": group_id},
                result="simulated",
            )
            continue
        try:
            client.post(
                f"{GRAPH_BASE_URL}/groups/{group_id}/members/$ref",
                json_body={"@odata.id": f"{GRAPH_BASE_URL}/directoryObjects/{user_id}"},
            )
            audit(
                "add_to_group",
                before={"member_of": False, "group_id": group_id},
                after={"member_of": True, "group_id": group_id},
                result="success",
            )
        except GraphError as exc:
            audit(
                "add_to_group",
                before={"member_of": False, "group_id": group_id},
                after=None,
                result="failed",
            )
            logger.error("Adding to group %s failed, continuing onboarding: %s", group_id, exc)

    # --- Assign license ---
    if dry_run:
        audit(
            "assign_license",
            before={"licensed": False},
            after={"licensed": True, "sku_id": license_sku_id},
            result="simulated",
        )
    else:
        try:
            client.post(
                f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}/assignLicense",
                json_body={"addLicenses": [{"skuId": license_sku_id}], "removeLicenses": []},
            )
            audit(
                "assign_license",
                before={"licensed": False},
                after={"licensed": True, "sku_id": license_sku_id},
                result="success",
            )
        except GraphError as exc:
            audit(
                "assign_license",
                before={"licensed": False},
                after=None,
                result="failed",
            )
            logger.error("License assignment failed: %s", exc)

    return OnboardingResult(
        user_id=user_id, temporary_password=temporary_password, entries=entries
    )


def _reverse_create_user(client: GraphClient, user_id: str, entry: AuditEntry) -> None:
    """Undo "create" by disabling, not deleting - see module docstring."""
    client.patch(f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}", json_body={"accountEnabled": False})


def _reverse_add_to_group(client: GraphClient, user_id: str, entry: AuditEntry) -> None:
    group_id = entry.after["group_id"]
    client.delete(f"{GRAPH_BASE_URL}/groups/{group_id}/members/{user_id}/$ref")


def _reverse_assign_license(client: GraphClient, user_id: str, entry: AuditEntry) -> None:
    sku_id = entry.after["sku_id"]
    client.post(
        f"{GRAPH_BASE_URL}{USERS_PATH}/{user_id}/assignLicense",
        json_body={"addLicenses": [], "removeLicenses": [sku_id]},
    )


ONBOARDING_REVERSERS: dict[str, ReverseFn] = {
    "create_user": _reverse_create_user,
    "add_to_group": _reverse_add_to_group,
    "assign_license": _reverse_assign_license,
}


def _run_rollback(args: argparse.Namespace, dry_run: bool) -> int:
    try:
        resolved_timestamp, entries = find_run_entries(
            DEFAULT_AUDIT_LOG_PATH, args.user_principal_name, timestamp=args.timestamp
        )
    except RollbackTargetNotFound as exc:
        logger.error(str(exc))
        return 1

    if dry_run:
        client = GraphClient(access_token="unused-in-dry-run")
    else:
        try:
            config = load_graph_config()
            token = get_access_token(config)
        except RuntimeError as exc:
            logger.error(str(exc))
            return 1
        client = GraphClient(access_token=token)

    outcomes = run_rollback(
        client,
        entries,
        reversers=ONBOARDING_REVERSERS,
        resolve_user_id_url=f"{GRAPH_BASE_URL}{USERS_PATH}/{args.user_principal_name}",
        operator=args.operator,
        dry_run=dry_run,
        audit_log_path=DEFAULT_AUDIT_LOG_PATH,
    )

    print(f"Rolling back run at {resolved_timestamp} for {args.user_principal_name}:")
    for outcome in outcomes:
        print(f"  {outcome.action}: {outcome.rollback_result} (was {outcome.original_result})")

    return 0


def main(argv: list[str] | None = None, confirm_fn: Callable[[str], str] = input) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    parser = argparse.ArgumentParser(
        description="Onboard a new hire, or roll one back (Part B, dry-run by default)."
    )
    parser.add_argument("--display-name")
    parser.add_argument("--user-principal-name", required=True)
    parser.add_argument("--department")
    parser.add_argument("--license-sku-id")
    parser.add_argument("--operator", required=True, help="Human identity to attribute this run to.")
    parser.add_argument(
        "--department-groups",
        type=Path,
        default=DEFAULT_DEPARTMENT_GROUPS_PATH,
        help="Path to the department -> group-IDs JSON config.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Make real changes. Without this, the run is a dry run (default).",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Reverse a prior onboarding run instead of creating a new one.",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Exact run timestamp to roll back (--rollback only; default: most "
        "recent run for --user-principal-name).",
    )
    args = parser.parse_args(argv)

    if not args.rollback and not (args.display_name and args.department and args.license_sku_id):
        parser.error(
            "--display-name, --department, and --license-sku-id are required "
            "unless --rollback is set"
        )

    dry_run = not args.execute

    if not dry_run:
        prompt_action = (
            f"rolling back changes for {args.user_principal_name}"
            if args.rollback
            else f"creating {args.user_principal_name}"
        )
        if not confirm_retype(
            args.user_principal_name,
            f"Type the exact user principal name to confirm {prompt_action} for real: ",
            confirm_fn=confirm_fn,
        ):
            record_audit_entry(
                AuditEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    operator=args.operator,
                    action="rollback_aborted" if args.rollback else "onboarding_aborted",
                    dry_run=False,
                    target=args.user_principal_name,
                    before=None,
                    after=None,
                    result="aborted",
                ),
                path=DEFAULT_AUDIT_LOG_PATH,
            )
            print("Confirmation did not match - aborted, no changes made.")
            return 1

    if args.rollback:
        return _run_rollback(args, dry_run)

    department_groups = load_department_group_mapping(args.department_groups)

    if dry_run:
        # Dry run never calls Graph (see onboard_user) - no reason to force
        # a real auth round-trip just to plan and audit what *would* happen.
        client = GraphClient(access_token="unused-in-dry-run")
    else:
        try:
            config = load_graph_config()
            token = get_access_token(config)
        except RuntimeError as exc:
            logger.error(str(exc))
            return 1
        client = GraphClient(access_token=token)

    result = onboard_user(
        client,
        display_name=args.display_name,
        user_principal_name=args.user_principal_name,
        department=args.department,
        license_sku_id=args.license_sku_id,
        department_groups=department_groups,
        operator=args.operator,
        dry_run=dry_run,
    )

    if dry_run:
        print(f"Dry run complete - {len(result.entries)} action(s) planned, none executed.")
    elif result.temporary_password is not None:
        print(f"Onboarding complete - user id {result.user_id}, {len(result.entries)} action(s).")
        print(
            f"Temporary password (shown once, never logged or audited): "
            f"{result.temporary_password}"
        )
        print("Hand this off to the new hire out-of-band now - it will not be shown again.")
    else:
        print("User creation failed - see the log above. No temporary password was generated.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
